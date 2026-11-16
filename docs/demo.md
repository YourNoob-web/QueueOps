# Job walkthrough

Run these examples from the project root with the Compose stack running.
They create demo records and retain them; they never call a paid API.

## Complete a job

```sh
curl --fail -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"job_name":"demo-sum","task_type":"sum_numbers","payload":{"numbers":[10,20,30]},"priority":"high"}'
```

Copy the returned `job_id`, then fetch `GET /jobs/{job_id}`. The worker should
record `status: completed`, `attempts: 1`, and `result: {"total": 60}`.

## Reproduce a failure and bounded manual retries

The example below creates one `simulate_failure` job, waits for each execution,
retries twice, and verifies that another retry returns HTTP 409 after three
attempts. It uses only Python 3.12's standard library and retains the demo job.

```sh
python3.12 - <<'PY'
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8000"
def api(path, data=None):
    body = None if data is None else json.dumps(data).encode()
    request = Request(base + path, data=body,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)

job = api("/jobs", {"job_name": "retry-demo",
                    "task_type": "simulate_failure", "payload": {}})
path = "/jobs/" + job["job_id"]
print("Job:", job["job_id"])
for attempt in range(1, 4):
    if attempt > 1:
        api(path + "/retry", {})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = api(path)
        if job["status"] == "failed" and job["attempts"] == attempt:
            print("Failed attempt:", attempt)
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Worker did not reach the expected failed state")
try:
    api(path + "/retry", {})
except HTTPError as error:
    assert error.code == 409, error.code
    print("Retry limit enforced: HTTP 409")
else:
    raise RuntimeError("Expected the retry limit to reject another attempt")
PY
```

Each attempt should record `failed` and the simulated error. Retries are manual;
the bound is tested for sequential use. Concurrent retry requests are not atomic.
See [failure semantics](architecture.md#failure-semantics).

## Inspect live monitoring

Open [Prometheus](http://127.0.0.1:9090) and inspect `up{job="queueops-api"}`;
the intended `api:8000` target should be healthy. In
[Grafana](http://127.0.0.1:3000), sign in with your local credentials and open
Explore with the provisioned **Prometheus** datasource. Try:

- `queueops_jobs_submitted_total`
- `queueops_http_requests_total`
- `rate(queueops_http_request_duration_seconds_sum[5m])`

The screenshots show real local results. Grafana Explore is supplied by Grafana;
this project provisions its datasource, not a saved dashboard or alert rules.

## Verify the whole stack

```sh
python3.12 scripts/verify_stack.py
```

This creates two more demo records and checks PostgreSQL/Redis readiness, a
successful RQ execution, failures and the retry ceiling, a fresh Prometheus scrape,
and Grafana datasource health. It does not inspect concurrent retry behavior.

## Stop safely

`docker compose stop` retains containers and named volumes. To remove containers
and the network, use `docker compose down` without `-v`. Named volumes survive,
but Redis queue state may be lost because Redis has no persistent data volume.
Never delete existing volumes to resolve credential mismatches.
