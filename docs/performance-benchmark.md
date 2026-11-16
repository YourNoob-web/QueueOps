# Local performance benchmark

`scripts/benchmark_queueops.py` measures the existing FastAPI → PostgreSQL →
Redis/RQ → PostgreSQL path. It never scales services, deletes records, flushes
Redis, or calls a paid API. Benchmark jobs remain in the local database.

## Workload and measurement

- Each measured round submits 40 `sum_numbers` jobs, each containing integers
  1–100 with expected result 5050, at normal priority and submission concurrency 8.
- Six warm-up jobs run outside timing before each measured round.
- Three rounds per configuration use pair order 1→3, 3→1, 1→3: the actual worker
  sequence is 1, 3, 3, 1, 1, 3. This reduces a fixed one-then-three ordering bias.
- The script matches the actual registered RQ consumers and all three queue
  subscriptions to running Compose worker hostnames, before and after each round.
  A host-run or unexpected consumer invalidates the run.
- Queues and workers must be idle, and no existing database jobs may be queued or
  running. Other active jobs cause refusal; they are never removed.
- Every reported job must complete once with result 5050. Errors and timeouts
  invalidate a batch rather than being silently excluded.

**Throughput** is completed jobs divided by monotonic elapsed time from the first
submission thread starting until the final completion is observed through the API. This is
end-to-end batch throughput, including submission and polling overhead, not an
isolated worker-capacity measurement. Polling uses paginated job lists at a 50 ms
interval rather than one request per job.

**P50/P95 job lifecycle latency** is `updated_at − created_at` for completed jobs.
These timestamps share the Docker VM clock. This includes queue wait and execution,
but excludes HTTP work before record creation and final commit visibility delay.
It is not worker-only execution time or client request latency. Client-observed
latencies are recorded separately in the JSON output.

Percentiles use linear interpolation at index `(n−1) × percentile/100`, pooled
across all 120 measured jobs per configuration. Throughput is the median of three
per-round rates. Scaling change is `(median_3 / median_1 − 1) × 100`; a negative
value is reported as a slowdown. Per-round rates and their range are retained.

## Reproduce

Start the stack from the project root using the README. Keep the machine otherwise
idle, stop additional host-run RQ consumers, and verify there is no pending work.
These commands assume the original configuration has one worker. Scaling changes
only the worker service; existing API, database, Redis and monitoring containers
and volumes are preserved. No application rebuild is required.

```sh
(
set -eu
bench_dir=$(mktemp -d /tmp/queueops-bench.XXXXXX)
for workers in 1 3 3 1 1 3; do
  docker compose up -d --no-deps --no-build --scale worker="$workers" worker
  if [ -f "$bench_dir/$workers.json" ]; then
    .venv/bin/python scripts/benchmark_queueops.py --workers "$workers" \
      --jobs 40 --runs 1 --append --out "$bench_dir/$workers.json"
  else
    .venv/bin/python scripts/benchmark_queueops.py --workers "$workers" \
      --jobs 40 --runs 1 --out "$bench_dir/$workers.json"
  fi
done
.venv/bin/python scripts/benchmark_queueops.py \
  --compare "$bench_dir/1.json" "$bench_dir/3.json"
docker compose up -d --no-deps --no-build --scale worker=1 worker
)
```

The subshell stops at the first failed command, before any further scaling.
On failure, leave the current workers running, retain the output and records,
let in-flight jobs finish, then manually restore the original worker count.
Never use `down -v`, truncate tables or flush Redis.
The script refuses to overwrite an existing result unless `--append` is supplied;
append and comparison require matching workload, source and environment metadata.
Raw results under `artifacts/` are ignored by Git. Review any output before sharing.

## Measured local results

Measured on November 15, 2026 (America/Los_Angeles), with an Apple M4 Mac
(10 logical CPUs, 16 GiB host RAM), macOS 15.6, Python 3.12.16, Docker Engine
29.1.2, and a Linux ARM64 Docker VM allocated 10 CPUs and 7.65 GiB RAM.
The running API and worker source files matched the checkout. The application
revision was `6c9ddafd12b0301ca75a3cea72e16d166bf0235b`; no runtime code changed.

| Verified workers | Median batch jobs/sec | Per-round range | P50 lifecycle | P95 lifecycle | Jobs validated |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 40.14 | 28.65–42.82 | 496.7 ms | 1009.5 ms | 120 |
| 3 | 64.52 | 45.94–68.00 | 245.7 ms | 439.3 ms | 120 |

Observed median throughput change: **+60.7%**.
P95 lifecycle latency was **56.5% lower**. These are measured
configuration differences, not proof of a general production scaling factor.

| Configuration round | Workers | Batch seconds | Jobs/sec | P50 lifecycle ms | P95 lifecycle ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 1 | 0.934 | 42.82 | 420.6 | 749.5 |
| 2 | 1 | 1.396 | 28.65 | 647.8 | 1104.0 |
| 3 | 1 | 0.997 | 40.14 | 447.7 | 782.8 |
| 1 | 3 | 0.588 | 68.00 | 222.8 | 341.1 |
| 2 | 3 | 0.620 | 64.52 | 236.5 | 340.5 |
| 3 | 3 | 0.871 | 45.94 | 323.6 | 506.7 |

For comparison, client-observed P50/P95 (submission start through API polling)
were 593.1/1080.9 ms with one worker and 321.0/509.0 ms with three. These are
separate from the persisted lifecycle values in the table above.

All 240 measured jobs completed on the first attempt with result 5050, with no
failed or timed-out measured jobs. Worker registrations matched the exact
Compose counts and hostnames before/after every round. The original single
worker was restored; all 12 original database records and the five other service
container IDs were unchanged. The 36 warm-up jobs and 240 measured jobs remain
in the local database (288 total records afterward).

The host was not an isolated benchmark machine, and batch rates varied visibly.
These results should be described as **local 40-job synthetic batches**, not
sustained capacity or production response-time guarantees. No alternate runs were
dropped or selectively rerun to improve the comparison.

Raw results are retained locally under
`artifacts/benchmarks/20261116T062704Z/` as `1-workers.json`, `3-workers.json`,
and `comparison.json`. The output includes every measured job's timestamps,
validated result, worker snapshots, settings and source fingerprints. That directory
is ignored by Git. The measured benchmark script SHA256 is
`1141b5650db17203cde610ce28c12378fbcab1cf9282775d7d326da272697253`.

## Interpretation

This tiny deterministic task mainly exercises HTTP, RQ process creation, queue
coordination and database overhead. It does not establish CPU scaling, maximum
sustained capacity, production P95 latency or statistical significance from three
rounds. Host activity, Docker scheduling, polling and the short batch size all
influence results. Worker scaling is a configuration experiment; it is not a code
optimization claim.
