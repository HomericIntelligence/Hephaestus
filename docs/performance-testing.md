# Performance Testing

The dedicated bounded worker-pool load lane no longer exists. The non-core-library
test re-scope removed the whole load-test directory, its `conftest.py`, and
every test that measured scheduling capacity, completion latency, and sustained
concurrency through the real submit, executor, callback, and `CompletionQueue`
path.

## What remains

Three artifacts of the lane survive. A future operator can reuse them.

- The `performance` pytest marker stays registered in the `[tool.pytest.ini_options]`
  `markers` list. It reads
  `bounded capacity, latency, and sustained-concurrency tests`.
- The `justfile` target, the nightly workflow, and the release workflow still
  deselect the lane with `-m "not performance"`. These selections are inert
  while no test carries the marker.
- The `precommit` marker remains the default selection, so an operator must pass
  `--override-ini="addopts="` to run any lane that the default deselects.

No `--load-duration-s`, `--load-max-jobs`, `--load-workers`, `--load-max-in-flight`,
`--load-service-ms`, `--load-p95-budget-ms`, or `--load-report` option is
registered in `tests/conftest.py`. The command that this page used to publish
now fails with an unknown-option error.

## Reintroducing a lane

An operator who reintroduces a load lane owns all of the following.

1. Register every load option in `pytest_addoption()` in `tests/conftest.py`.
2. Place the lane under a path in `testpaths`, or add an explicit path to the
   command, because the old directory sat outside collection.
3. Restore the bounded configuration checks. The lane must reject non-positive
   values and must cap the submission window, the job count, the worker count,
   the in-flight count, the synthetic service time, and the p95 budget.
   The in-flight cap must be at least the worker count.
4. Restore the gate. Every submitted job must produce exactly one successful
   completion, with no duplicates, with every configured worker active under
   sustained load, with a bounded drain, with end-to-end p95 latency inside the
   budget, and with four-worker capacity at least twice the one-worker control.
5. Restore a runtime evidence report. Do not commit or hand-author report
   values. Read the generated artifact instead.

## Percentile method

Keep the previous method if a lane returns percentiles. The old lane used
linear interpolation between closest ranks (`rank = percentile * (n - 1)`,
the numpy `linear` method). That method is tail-inclusive. A nearest-rank
formula that rounds down into the fast body can hide a small fraction of very
slow completions below a p95 gate (issue #2229).

## Why this page remains

`tests/unit/ci/test_workflows.py::TestPerformanceWorkflow` requires this file
and the link to it in [`index.md`](index.md). The page records the removed lane
so that the surviving marker, the inert selections, and the missing load options
are not mistaken for an oversight.
