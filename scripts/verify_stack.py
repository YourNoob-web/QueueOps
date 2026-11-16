"""Local integration check. Creates two demo jobs; never calls OpenAI or deletes data."""

import base64
import json
import subprocess
import time
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API = "http://127.0.0.1:8000"
PROMETHEUS = "http://127.0.0.1:9090"


def request(url, data=None, headers=None, expected=200):
    headers = headers or {}
    encoded = None if data is None else json.dumps(data).encode()
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=encoded, headers=headers), timeout=10) as response:
            assert response.status == expected, "Unexpected HTTP status"
            return json.loads(response.read())
    except HTTPError as exc:
        if exc.code == expected:
            return json.loads(exc.read())
        raise RuntimeError(f"HTTP check failed with status {exc.code}") from None


def wait_job(job_id, state, attempts):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = request(f"{API}/jobs/{job_id}")
        if job["status"] == state and job["attempts"] == attempts:
            return job
        time.sleep(0.5)
    raise RuntimeError("Worker did not reach the expected state within 30 seconds")


def main():
    assert request(f"{API}/ready")["status"] == "ready"
    print("PASS: API readiness (database and Redis)")
    job = request(f"{API}/jobs", {
        "job_name": "integration-success", "task_type": "sum_numbers",
        "payload": {"numbers": [10, 20, 30]}, "priority": "high",
    }, expected=201)
    assert wait_job(job["job_id"], "completed", 1)["result"]["total"] == 60
    print("PASS: job submitted through Redis and processed by worker")
    failed = request(f"{API}/jobs", {
        "job_name": "integration-failure", "task_type": "simulate_failure", "payload": {},
    }, expected=201)
    for attempt in range(1, 4):
        if attempt > 1:
            request(f"{API}/jobs/{failed['job_id']}/retry", {}, expected=202)
        wait_job(failed["job_id"], "failed", attempt)
    request(f"{API}/jobs/{failed['job_id']}/retry", {}, expected=409)
    print("PASS: failed jobs and sequential three-attempt retry limit")
    # Require a fresh successful scrape of the intended target, rather than
    # accepting an old time series left in the persistent Prometheus volume.
    scrape_after = time.time()
    deadline = time.monotonic() + 45
    while True:
        targets = request(f"{PROMETHEUS}/api/v1/targets?state=active")
        current_target = any(
            target.get("labels", {}).get("job") == "queueops-api"
            and target.get("scrapeUrl") == "http://api:8000/metrics"
            and target.get("health") == "up"
            and datetime.fromisoformat(target["lastScrape"].replace("Z", "+00:00")).timestamp() >= scrape_after
            for target in targets.get("data", {}).get("activeTargets", [])
        )
        selector = '{job="queueops-api",instance="api:8000"}'
        up = request(f"{PROMETHEUS}/api/v1/query?" + urlencode({"query": "up" + selector}))
        submitted = request(f"{PROMETHEUS}/api/v1/query?" + urlencode({
            "query": "sum(queueops_jobs_submitted_total" + selector + ")",
        }))
        up_values = up.get("data", {}).get("result", [])
        submitted_values = submitted.get("data", {}).get("result", [])
        if (
            current_target
            and any(float(item["value"][1]) == 1 for item in up_values)
            and any(float(item["value"][1]) >= 2 for item in submitted_values)
        ):
            break
        if time.monotonic() > deadline:
            raise RuntimeError("Prometheus did not freshly scrape the healthy API target and submitted jobs")
        time.sleep(1)
    print("PASS: Prometheus freshly scrapes api:8000 with up=1 and at least two submitted jobs")
    # Capture the resolved configuration privately; never print credentials.
    config_result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"], capture_output=True, text=True,
    )
    if config_result.returncode:
        raise RuntimeError("Could not resolve local Compose configuration")
    env = json.loads(config_result.stdout)["services"]["grafana"]["environment"]
    credentials = f"{env['GF_SECURITY_ADMIN_USER']}:{env['GF_SECURITY_ADMIN_PASSWORD']}"
    auth = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
    health = request("http://127.0.0.1:3000/api/datasources/uid/prometheus/health", headers=auth)
    assert health["status"] == "OK", "Grafana datasource health check failed"
    print("PASS: Grafana authenticates and connects to Prometheus")
    print("Integration checks passed. Two demo job records were retained.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Avoid exception text or response bodies that might contain local secrets.
        raise SystemExit(
            f"FAIL: integration check stopped ({type(exc).__name__}). "
            "Check local service health; an existing Grafana volume may use a different admin password."
        ) from None
