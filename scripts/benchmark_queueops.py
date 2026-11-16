"""Benchmark the existing local stack; retains synthetic jobs, never scales or deletes."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


API = "http://127.0.0.1:8000"
QUEUES = ["queueops-high", "queueops-normal", "queueops-low"]


class BenchmarkError(Exception):
    pass


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise BenchmarkError("Docker/Git command failed; diagnostic output withheld.")
    return result.stdout.strip()


def request(path, data=None):
    encoded = None if data is None else json.dumps(data).encode()
    try:
        with urlopen(Request(API + path, data=encoded, headers={
            "Content-Type": "application/json",
        }), timeout=10) as response:
            if response.status != (200 if data is None else 201):
                raise BenchmarkError("Unexpected API status.")
            return json.load(response)
    except Exception:
        raise BenchmarkError("API request failed; response details withheld.") from None


def percentile(values, percent):
    """Inclusive linear interpolation at index (n - 1) * percent / 100."""
    if not values or not 0 <= percent <= 100:
        raise BenchmarkError("Invalid percentile input.")
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower, upper = math.floor(index), math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def job_metrics(job, started, observed):
    if job["status"] != "completed" or job["attempts"] != 1 or job["result"] != {"total": 5050}:
        raise BenchmarkError("A benchmark job failed result/status/attempt validation.")
    created = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
    updated = datetime.fromisoformat(job["updated_at"].replace("Z", "+00:00"))
    lifecycle = (updated - created).total_seconds() * 1000
    client = (observed - started) * 1000
    if lifecycle < 0 or client < 0 or not all(map(math.isfinite, (lifecycle, client))):
        raise BenchmarkError("Invalid benchmark timestamp ordering.")
    return {key: job[key] for key in (
        "job_id", "job_name", "status", "attempts", "result", "created_at", "updated_at",
    )} | {"lifecycle_ms": lifecycle, "client_observed_ms": client}


def worker_snapshot(expected, wait_idle=False):
    ids = command(["docker", "compose", "ps", "-q", "worker"]).split()
    if len(ids) != expected:
        raise BenchmarkError("Compose worker count differs from --workers; script does not scale services.")
    inspected = command(["docker", "inspect", "--format", "{{.Config.Hostname}} {{.State.Running}}", *ids])
    rows = [line.split() for line in inspected.splitlines()]
    if any(len(row) != 2 or row[1] != "true" for row in rows):
        raise BenchmarkError("A Compose worker is not running.")
    hostnames = sorted(row[0] for row in rows)
    probe = """
import json, os
from redis import Redis
from rq import Queue, Worker
from rq.serializers import JSONSerializer
connection = Redis.from_url(os.environ['REDIS_URL'])
names = ['queueops-high', 'queueops-normal', 'queueops-low']
workers = [{'hostname': w.hostname, 'queues': w.queue_names(), 'state': w.get_state()}
           for w in Worker.all(connection=connection, serializer=JSONSerializer)
           if set(w.queue_names()).intersection(names)]
print(json.dumps({'workers': workers, 'queued': sum(Queue(n, connection=connection).count for n in names)}))
"""
    deadline = time.monotonic() + (15 if wait_idle else 0)
    while True:
        snapshot = json.loads(command(["docker", "exec", ids[0], "python", "-c", probe]))
        workers = snapshot["workers"]
        registered = [w["hostname"] for w in workers]
        if (set(registered) - set(hostnames) or len(registered) != len(set(registered))
                or any(w["queues"] != QUEUES for w in workers)):
            raise BenchmarkError("Registered QueueOps consumers do not exactly match Compose workers/queues.")
        if sorted(registered) == hostnames and snapshot["queued"] == 0 and all(w["state"] == "idle" for w in workers):
            return snapshot
        if time.monotonic() >= deadline:
            raise BenchmarkError("QueueOps queues/workers are not idle; existing work was left intact.")
        time.sleep(0.1)


def assert_no_active_jobs():
    for state in ("queued", "running"):
        if request("/jobs?" + urlencode({"status": state, "limit": 1}))["count"]:
            raise BenchmarkError("Existing queued/running database jobs prevent an isolated benchmark.")


def run_batch(jobs, concurrency, timeout, poll_seconds):
    prefix = "benchmark-" + uuid4().hex[:12] + "-"
    started_at = datetime.now(timezone.utc).isoformat()

    def submit(index):
        start = time.perf_counter()
        job = request("/jobs", {
            "job_name": prefix + str(index), "task_type": "sum_numbers",
            "payload": {"numbers": list(range(1, 101))}, "priority": "normal",
        })
        return job["job_id"], start

    submitted, completed = {}, {}
    last_observed = None
    batch_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        pending = {pool.submit(submit, index) for index in range(jobs)}
        while len(completed) < jobs:
            if time.perf_counter() - batch_start > timeout:
                raise BenchmarkError("Benchmark timed out; submitted demo jobs were retained.")
            for future in list(pending):
                if future.done():
                    job_id, start = future.result()
                    if job_id in submitted:
                        raise BenchmarkError("API returned a duplicate job ID.")
                    submitted[job_id] = start
                    pending.remove(future)
            offset = 0
            missing = set(submitted) - set(completed)
            while True:
                page = request("/jobs?" + urlencode({"limit": 100, "offset": offset}))
                observed = time.perf_counter()
                for job in page["jobs"]:
                    if job["status"] in ("queued", "running") and not job["job_name"].startswith(prefix):
                        raise BenchmarkError("Unrelated active jobs appeared during the benchmark.")
                    job_id = job["job_id"]
                    if job_id not in missing:
                        continue
                    missing.remove(job_id)
                    if job["status"] in ("failed", "queue_error"):
                        raise BenchmarkError("A benchmark job failed; demo records were retained.")
                    if job["status"] == "completed":
                        completed[job_id] = job_metrics(job, submitted[job_id], observed)
                        last_observed = observed
                # Usually one list request; paginate only when submitted IDs are
                # displaced by newer records. Never assume the database is empty.
                offset += 100
                if not missing or pending or offset >= page["count"]:
                    break
            if len(completed) < jobs:
                time.sleep(poll_seconds)
    if len(submitted) != jobs or len(completed) != jobs:
        raise BenchmarkError("Incomplete batch; no result will be reported as valid.")
    elapsed = last_observed - min(submitted.values())
    return {"started_at": started_at, "elapsed_seconds": elapsed, "jobs_per_second": jobs / elapsed,
            "jobs": sorted(completed.values(), key=lambda job: job["job_name"])}


def summarize(document):
    rounds = document["rounds"]
    if not rounds:
        raise BenchmarkError("Benchmark has no completed rounds.")
    jobs = [job for run in rounds for job in run["jobs"]]
    rates = [run["jobs_per_second"] for run in rounds]
    return {"workers": document["workers"], "rounds": len(rounds), "validated_jobs": len(jobs),
            "median_jobs_per_second": statistics.median(rates), "throughput_range": [min(rates), max(rates)],
            "lifecycle_p50_ms": percentile([job["lifecycle_ms"] for job in jobs], 50),
            "lifecycle_p95_ms": percentile([job["lifecycle_ms"] for job in jobs], 95),
            "client_observed_p50_ms": percentile([job["client_observed_ms"] for job in jobs], 50),
            "client_observed_p95_ms": percentile([job["client_observed_ms"] for job in jobs], 95)}


def validate_compatible(first, second, append=False):
    if any(first[key] != second[key] for key in ("schema_version", "settings", "environment")):
        raise BenchmarkError("Benchmark settings, source fingerprint or environment differ.")
    if append and first["workers"] != second["workers"]:
        raise BenchmarkError("Cannot append a different worker configuration.")


def compare(first, second):
    validate_compatible(first, second)
    if {first["workers"], second["workers"]} != {1, 3}:
        raise BenchmarkError("Comparison requires one-worker and three-worker results.")
    one, three = sorted((first, second), key=lambda doc: doc["workers"])
    if len(one["rounds"]) != len(three["rounds"]):
        raise BenchmarkError("Comparison requires equal measured round counts.")
    a, b = summarize(one), summarize(three)
    return {"one_worker": a, "three_workers": b,
            "throughput_change_percent": (b["median_jobs_per_second"] / a["median_jobs_per_second"] - 1) * 100,
            "lifecycle_p50_reduction_percent": (1 - b["lifecycle_p50_ms"] / a["lifecycle_p50_ms"]) * 100,
            "lifecycle_p95_reduction_percent": (1 - b["lifecycle_p95_ms"] / a["lifecycle_p95_ms"]) * 100}


def environment():
    inputs = ["app/main.py", "app/worker.py", "compose.yaml", "requirements.txt"]
    fingerprint = hashlib.sha256(b"".join(name.encode() + b"\0" + Path(name).read_bytes() for name in inputs)).hexdigest()
    docker = json.loads(command(["docker", "info", "--format",
        '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}},"architecture":{{json .Architecture}},"os":{{json .OSType}}}']))
    return {"platform": platform.platform(), "python": platform.python_version(), "docker": docker,
            "git_commit": command(["git", "rev-parse", "HEAD"]), "source_sha256": fingerprint,
            "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def write_result(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 3))
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--poll-seconds", type=float, default=0.05)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.compare:
        print(json.dumps(compare(*(json.loads(path.read_text()) for path in args.compare)), indent=2))
        return
    if args.workers is None or not 1 <= args.jobs <= 100 or min(args.runs, args.concurrency, args.warmup) < 1:
        parser.error("--workers is required; jobs must be 1..100 and runs/concurrency/warmup positive")
    if not 0.01 <= args.poll_seconds <= 1 or not 1 <= args.timeout <= 3600:
        parser.error("poll interval must be 0.01..1 seconds; timeout must be 1..3600 seconds")
    path = args.out or Path(f"artifacts/benchmark-{args.workers}-workers.json")
    document = {"schema_version": 1, "workers": args.workers, "environment": environment(), "rounds": [],
        "settings": {"jobs": args.jobs, "concurrency": args.concurrency, "warmup": args.warmup,
                     "timeout": args.timeout, "poll_seconds": args.poll_seconds,
                     "task_type": "sum_numbers", "numbers": "1..100", "priority": "normal"}}
    if path.exists():
        if not args.append:
            raise BenchmarkError("Output already exists; use --append only for matching results.")
        existing = json.loads(path.read_text())
        validate_compatible(existing, document, append=True)
        document = existing
    elif args.append:
        raise BenchmarkError("Append output does not exist; run the first round without --append.")
    if request("/ready").get("status") != "ready":
        raise BenchmarkError("API is not ready.")
    worker_snapshot(args.workers, wait_idle=True)
    assert_no_active_jobs()
    run_batch(args.warmup, args.concurrency, args.timeout, args.poll_seconds)
    for _ in range(args.runs):
        before = worker_snapshot(args.workers, wait_idle=True)
        assert_no_active_jobs()
        result = run_batch(args.jobs, args.concurrency, args.timeout, args.poll_seconds)
        result["workers_before"] = before
        result["workers_after"] = worker_snapshot(args.workers, wait_idle=True)
        if sorted(w["hostname"] for w in before["workers"]) != sorted(
                w["hostname"] for w in result["workers_after"]["workers"]):
            raise BenchmarkError("A worker was replaced during the measured batch.")
        document["rounds"].append(result)
        write_result(path, document)
        print(json.dumps(summarize(document)), flush=True)
    print(f"Retained synthetic jobs and saved results to {path}.")


if __name__ == "__main__":
    try:
        main()
    except BenchmarkError as exc:
        raise SystemExit(f"FAIL: {exc}") from None
    except Exception as exc:
        raise SystemExit(f"FAIL: benchmark stopped ({type(exc).__name__}); details withheld.") from None
