# Performance Testing

The dedicated performance lane exercises the queue pipeline's `WorkerPool`.
It measures scheduling capacity, completion latency, and sustained concurrency
through the real submit, executor, callback, and `CompletionQueue` path.

The synthetic jobs replace only `WorkerPool._run_build_test` with a fixed
service delay. They do not invoke agents, subprocesses, GitHub, Git, or other
external services. This keeps the lane focused on the worker-pool concurrency
boundary and prevents credentials, network state, or process startup from
changing its result.

## Safety bounds

The weekly/manual workflow uses a 30-second submission window and permits at
most 50,000 jobs, 8 workers, and 64 in-flight jobs. The test configuration
rejects non-positive values and hard limits exceeding 60 seconds, 100,000 jobs,
32 workers, 256 in-flight jobs, 1,000 ms of synthetic service time, or a
60,000 ms p95 budget before a load run starts. The workflow job has a 10-minute
timeout. The in-flight cap must also be at least the worker count so every
configured worker can become active.

The lane gates on all submitted jobs producing exactly one successful
completion, no duplicates, use of every configured worker during sustained
load, a bounded drain, end-to-end p95 latency no higher than 500 ms, and
four-worker capacity at least twice the one-worker control.

Percentiles are computed by linear interpolation between closest ranks
(`rank = percentile * (n - 1)`, numpy's default `linear` method), which is
tail-inclusive: a small fraction of very slow completions cannot hide below
the p95 gate the way a nearest-rank formula rounding down into the fast body
would (issue #2229).

## Running locally

Run the same bounded profile used by CI explicitly; the directory is excluded
from normal pytest collection:

```bash
uv run pytest tests/performance --override-ini="addopts=" -v --strict-markers --load-duration-s=30 --load-max-jobs=50000 --load-workers=8 --load-max-in-flight=64 --load-service-ms=5 --load-p95-budget-ms=500 --load-report=build/performance/worker-pool.json
```

The stalled-consumer recovery regression is performance-marked and is
deselected by the default unit-suite command (`pyproject.toml` excludes
`performance`). A successful unit-suite run therefore does not validate this
regression; run the test explicitly when reporting evidence for that behavior:

```bash
uv run pytest tests/performance/test_worker_pool_load.py::test_worker_pool_stalled_consumer_preserves_and_recovers --override-ini="addopts=" -m performance --strict-markers -v
```

## Runtime evidence

The generated JSON report records schema version, profile, Python/platform and
commit metadata, completion invariants, concurrency, throughput, and p50/p95/
p99/max queue and end-to-end latency. It also retains the one-worker and
four-worker capacity controls plus their measured throughput ratio. GitHub
Actions uploads it as the `worker-pool-performance` artifact for 14 days, even
when a threshold fails.

Measurements are runtime evidence only. Do not commit or hand-author report
values; inspect the generated CI artifact instead.

## Locally captured verification evidence

The following commands were independently run on the reviewed head on
2026-07-31. Their exit status was 0 in each case. The unit-suite result is
reported separately from the performance-marked regression because the latter
is excluded by the default unit-suite selection.

Unit suite (the repository's stated unit-suite command):

```text
$ uv run pytest tests/unit -v
Required test coverage of 83.0% reached. Total coverage: 85.01%
================= 6380 passed, 6 skipped in 136.10s (0:02:16) ==================
```

Stalled-consumer recovery regression (the explicit command above):

```text
$ uv run pytest tests/performance/test_worker_pool_load.py::test_worker_pool_stalled_consumer_preserves_and_recovers --override-ini="addopts=" -m performance --strict-markers -v
tests/performance/test_worker_pool_load.py::test_worker_pool_stalled_consumer_preserves_and_recovers PASSED [100%]
============================== 1 passed in 0.30s ===============================
```
