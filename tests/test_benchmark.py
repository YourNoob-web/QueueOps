"""Benchmark math/validity checks; no Docker, network or real job submissions."""

import copy

import pytest

from scripts.benchmark_queueops import (
    BenchmarkError, compare, job_metrics, percentile, summarize, validate_compatible,
)


def document(workers, rates, latencies):
    return {"schema_version": 1, "workers": workers, "settings": {"jobs": 2, "concurrency": 8},
            "environment": {"source_sha256": "fixture", "docker": {"cpus": 4}},
            "rounds": [{"jobs_per_second": rate,
                        "jobs": [{"lifecycle_ms": value, "client_observed_ms": value + 10} for value in pair]}
                       for rate, pair in zip(rates, latencies)]}


def test_pooled_percentiles_and_median_throughput():
    result = summarize(document(1, [10, 50], [[10, 20], [100, 200]]))
    assert result["median_jobs_per_second"] == 30
    assert result["throughput_range"] == [10, 50]
    assert result["validated_jobs"] == 4
    assert result["lifecycle_p50_ms"] == 60
    assert result["lifecycle_p95_ms"] == pytest.approx(185)
    assert percentile([7], 95) == 7
    with pytest.raises(BenchmarkError):
        percentile([], 95)


def test_comparison_reports_slowdown_without_claiming_improvement():
    one = document(1, [10, 20, 30], [[10, 20]] * 3)
    three = document(3, [5, 10, 15], [[20, 40]] * 3)
    result = compare(three, one)
    assert result["throughput_change_percent"] == -50
    assert result["lifecycle_p50_reduction_percent"] == -100
    assert result["lifecycle_p95_reduction_percent"] == -100


@pytest.mark.parametrize("field", ["settings", "environment"])
def test_comparison_rejects_changed_workload_or_environment(field):
    one = document(1, [10], [[10, 20]])
    three = copy.deepcopy(one)
    three["workers"] = 3
    three[field]["changed"] = True
    with pytest.raises(BenchmarkError):
        compare(one, three)


def test_append_and_comparison_configuration_guards():
    one = document(1, [10], [[10, 20]])
    three = document(3, [20, 30], [[10, 20], [10, 20]])
    with pytest.raises(BenchmarkError):
        validate_compatible(one, three, append=True)
    with pytest.raises(BenchmarkError):
        compare(one, three)
    with pytest.raises(BenchmarkError):
        compare(one, one)


@pytest.mark.parametrize("change", [{"attempts": 2}, {"result": {"total": 99}}, {"status": "failed"},
                                  {"updated_at": "2026-11-16T00:00:00+00:00"}])
def test_job_validation_prevents_invalid_success_samples(change):
    job = {"job_id": "fixture", "job_name": "synthetic", "status": "completed", "attempts": 1,
           "result": {"total": 5050}, "created_at": "2026-11-16T00:00:01+00:00",
           "updated_at": "2026-11-16T00:00:01.250+00:00"}
    valid = job_metrics(job, 10, 10.5)
    assert valid["lifecycle_ms"] == 250
    assert valid["client_observed_ms"] == 500
    with pytest.raises(BenchmarkError):
        job_metrics(job | change, 10, 10.5)
