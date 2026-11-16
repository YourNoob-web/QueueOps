import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main
from app.main import redact_secrets


@pytest.mark.parametrize("message", [
    'password="example with spaces"',
    "postgresql://user:example-value@localhost/db",
    "Authorization: Bearer example-value",
    "-----BEGIN PRIVATE KEY-----\nexample-value\n-----END PRIVATE KEY-----",
])
def test_log_redaction(message):
    assert "example" not in redact_secrets(message)


def test_nested_incident_redaction():
    incident = {
        "job_name": "token=example-value",
        "metrics": {"nested": [{"api_key": "example-value"}], "latency_ms": 12},
    }
    sanitized = redact_secrets(incident)
    assert "example-value" not in json.dumps(sanitized)
    assert sanitized["metrics"]["latency_ms"] == 12


@pytest.mark.parametrize("provider_fails", [False, True])
def test_incident_provider_receives_redacted_data(tmp_path, monkeypatch, caplog, provider_fails):
    """Exercise the endpoint's actual provider boundary without any network calls."""
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'incident.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=test_engine))
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    calls = []

    def create_response(**kwargs):
        incident = json.loads(kwargs["input"])
        calls.append(incident)
        encoded = json.dumps(incident)
        for marker in ("name-marker", "url-marker", "log-marker", "nested-marker"):
            assert marker not in encoded
        assert incident["metrics"]["latency_ms"] == 12
        assert incident["task_type"] == "simulate_failure"
        if provider_fails:
            raise RuntimeError("provider-private-error-marker")
        return SimpleNamespace(output_text="Mock incident analysis")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create_response)),
    ))

    try:
        with TestClient(main.app) as client:
            with main.SessionLocal() as db:
                db.add(main.JobRecord(
                    id="incident-test", job_name="password=name-marker",
                    task_type="simulate_failure", payload={}, priority="normal",
                    status="failed", attempts=1,
                    error="postgresql://user:url-marker@localhost/db",
                    created_at=main.utc_now(), updated_at=main.utc_now(),
                ))
                db.commit()
            response = client.post("/incidents/analyze", json={
                "job_id": "incident-test",
                "log_excerpt": "Authorization: Bearer log-marker",
                "metrics_snapshot": {"nested": [{"api_key": "nested-marker"}], "latency_ms": 12},
            })
        assert len(calls) == 1
        assert response.status_code == (502 if provider_fails else 200)
        if not provider_fails:
            assert response.json()["automated_remediation"] is False
        assert "provider-private-error-marker" not in response.text
        assert "provider-private-error-marker" not in caplog.text
    finally:
        test_engine.dispose()
