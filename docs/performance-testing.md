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

## Running locally

Run the same bounded profile used by CI explicitly; the directory is excluded
from normal pytest collection:

```bash
pixi run pytest tests/performance --override-ini="addopts=" -v --strict-markers --load-duration-s=30 --load-max-jobs=50000 --load-workers=8 --load-max-in-flight=64 --load-service-ms=5 --load-p95-budget-ms=500 --load-report=build/performance/worker-pool.json
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
