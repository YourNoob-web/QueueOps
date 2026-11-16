
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False}
    )

    monkeypatch.setattr(main, "engine", test_engine)

    monkeypatch.setattr(
        main,
        "SessionLocal",
        sessionmaker(bind=test_engine)
    )

    class FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr(
        main, "redis_connection", FakeRedis()
    )

    monkeypatch.setattr(
        main, "enqueue_job", lambda job: None
    )

    with TestClient(main.app) as test_client:
        yield test_client

    test_engine.dispose()


def create_test_job(client, task_type="sum_numbers", payload=None):
    return client.post(
        "/jobs",
        json={
            "job_name": "test_job",
            "task_type": task_type,
            "payload": payload if payload is not None else {
                "numbers": [10, 20, 30]
            },
            "priority": "high"
        }
    )


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_job(client):
    response = create_test_job(client)

    assert response.status_code == 201
    assert response.json()["status"] == "queued"
    assert response.json()["job_id"]

    jobs = client.get("/jobs")
    assert jobs.status_code == 200
    assert jobs.json()["count"] == 1


def test_invalid_payload(client):
    response = create_test_job(
        client,
        payload={"numbers": ["invalid"]}
    )

    assert response.status_code == 422


def test_job_not_found(client):
    response = client.get("/jobs/invalid-id")

    assert response.status_code == 404


def test_successful_job_processing(client):
    response = create_test_job(client)
    job_id = response.json()["job_id"]

    main.process_job(job_id)

    result = client.get(f"/jobs/{job_id}").json()

    assert result["status"] == "completed"
    assert result["attempts"] == 1
    assert result["result"]["total"] == 60


def test_failure_and_retry_limit(client):
    response = create_test_job(
        client,
        task_type="simulate_failure",
        payload={}
    )

    job_id = response.json()["job_id"]

    for attempt in range(1, 4):
        if attempt > 1:
            retry = client.post(f"/jobs/{job_id}/retry")
            assert retry.status_code == 202

        with pytest.raises(RuntimeError):
            main.process_job(job_id)

    job = client.get(f"/jobs/{job_id}").json()

    assert job["status"] == "failed"
    assert job["attempts"] == 3

    failed_jobs = client.get("/jobs?status=failed")

    assert failed_jobs.status_code == 200
    assert failed_jobs.json()["count"] == 1

    retry = client.post(f"/jobs/{job_id}/retry")

    assert retry.status_code == 409
