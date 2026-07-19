# Observability: metrics, alerts, and SLOs

This document defines the monitoring surface for the queue-based automation
pipeline (`hephaestus.automation.pipeline`): the metrics it exports, the alert
rules it evaluates, the Service Level Objectives (SLOs) those metrics measure,
and who owns responding to a fired alert.

It closes the Section 9 audit finding (issue #2153): the pipeline already wrote
structured JSONL events and served a local `/metrics` + `/health` endpoint, but
job outcomes and stall state were not exported, and no document defined the
metric catalog, alert ownership, or SLOs.

The metric and alert names below are drift-guarded against the code by
`tests/unit/docs/test_observability_doc.py`: every `hephaestus_*` metric emitted
by `hephaestus/automation/pipeline/coordinator.py` and every alert rule defined
in `hephaestus/observability/alerts.py` must appear here, so this catalog cannot
silently fall out of sync with what the pipeline actually exposes.

## Enabling monitoring

Observability is **opt-in** and loopback-only. The base `import hephaestus`
surface never pulls the metrics stack; the socket, registry, and alert tracker
are constructed only when a metrics port is configured.

- **Enable it** by passing `--metrics-port PORT` to the automation loop
  (`hephaestus.automation.loop_runner`), or by setting
  `PipelineConfig.metrics_port` directly. `0` (the default) disables it.
- **`/metrics`** serves the Prometheus text exposition of every gauge and
  counter in the catalog below. The server binds a literal loopback address
  only (`hephaestus/observability/server.py`); it rejects any non-loopback
  host, so the unauthenticated diagnostic endpoint is never exposed off-host.
- **`/health`** serves the JSON snapshot returned by the coordinator's
  `_health_snapshot`: the same lifecycle fields as the metrics, plus a
  top-level `status` of `ok` (running) or `stopping` (shutdown requested).
- **Structured event log (JSONL).** When a loop runs, the coordinator writes a
  durable JSONL event log (default:
  `build/.issue_implementer/pipeline-events-<timestamp>-<pid>.jsonl`, set via
  `PipelineConfig.event_log_path`). Each metrics tick appends a
  `metrics_snapshot` record, and every alert transition appends an
  `alert_fired` or `alert_resolved` record carrying the alert `name`,
  `severity`, and `message`. These are the lines to cite when escalating.

The alert queue-depth threshold is configurable via
`PipelineConfig.alert_queue_depth_threshold` (default `100`); the stall
threshold defaults to `3`, matching the coordinator's own
`_STALL_TICKS_BEFORE_FORCE`.

## Metrics

All metrics are namespaced `hephaestus_`. Gauges reflect the latest tick's
value; counters accumulate over the coordinator process lifetime. Source column
points at the emission chokepoint in `coordinator.py`.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `hephaestus_pipeline_queue_depth` | gauge | `stage` | Queued work items waiting in each stage queue. |
| `hephaestus_pipeline_inflight_jobs` | gauge | — | Jobs currently owned by the worker pool. |
| `hephaestus_pipeline_inflight_per_repo` | gauge | `repo` | In-flight jobs partitioned by repository. |
| `hephaestus_pipeline_loops_total` | gauge | — | Reseed passes run by this coordinator process. |
| `hephaestus_pipeline_stalled_ticks` | gauge | — | Consecutive drain ticks without pipeline progress. |
| `hephaestus_circuit_breaker_state` | gauge | `name`, `state` | Circuit-breaker lifecycle state (the active state has value `1`, others `0`). |
| `hephaestus_pipeline_alert_active` | gauge | `name` | Whether a named alert condition is currently active (`1`) or resolved (`0`). |
| `hephaestus_pipeline_jobs_total` | counter | `stage`, `outcome` | Completed jobs by stage and outcome (`ok`, `failed`, `interrupted`). |
| `hephaestus_pipeline_agent_job_seconds_total` | counter | — | Cumulative agent-job wall-clock seconds (negative durations clamped to `0`). |

## Alerts

Alert rules read **only** live coordinator snapshot data — there are no
speculative rules. `hephaestus/observability/alerts.py` evaluates the current
snapshot each tick, and `AlertTracker` emits a durable transition (an
`alert_fired` / `alert_resolved` JSONL event and an update to
`hephaestus_pipeline_alert_active`) only when a condition changes state, so a
persistent degradation never produces repeated event spam.

| Alert | Severity | Condition | Owner | Runbook |
| --- | --- | --- | --- | --- |
| `circuit_breaker_open` | critical | Any circuit breaker in the snapshot reports `state == "open"`. | repository maintainer | [`docs/runbooks/automation-loop-crash.md`](runbooks/automation-loop-crash.md) |
| `queue_depth_exceeds` | warning | Any stage queue depth exceeds `alert_queue_depth_threshold` (default `100`). | repository maintainer | [`docs/runbooks/ci-driver-stall.md`](runbooks/ci-driver-stall.md) |
| `pipeline_stalled` | warning | `stalled_ticks` reaches the stall threshold (default `3`) — drain ticks with no progress. | repository maintainer | [`docs/runbooks/ci-driver-stall.md`](runbooks/ci-driver-stall.md) |

## SLOs

Each SLO names the metric that measures it, so compliance is checked against
real exported data rather than intent. Targets are per automation run.

| SLO | Target | Measured by |
| --- | --- | --- |
| Pipeline liveness | `/health` reports `status: ok` for ≥ 99% of ticks during a run. | `/health` snapshot `status` field. |
| Stall budget | `hephaestus_pipeline_stalled_ticks` < 3 for ≥ 99% of ticks. | `hephaestus_pipeline_stalled_ticks` gauge. |
| Queue depth | `hephaestus_pipeline_queue_depth` ≤ the configured threshold for ≥ 99% of ticks. | `hephaestus_pipeline_queue_depth` gauge. |
| Agent job success rate | `hephaestus_pipeline_jobs_total{outcome="ok"}` / total completed jobs ≥ 90% per run. | `hephaestus_pipeline_jobs_total` counter. |
| Breaker budget | `circuit_breaker_open` fires ≤ 1 time per run. | `hephaestus_pipeline_alert_active{name="circuit_breaker_open"}` gauge / `alert_fired` events. |

These are **targets**, not committed measurements. Their ongoing measurement
happens operationally against the scrape endpoint below; this document defines
what "healthy" means, and the drift test keeps the definitions honest against
the code.

## Ownership and escalation

This is a single-maintainer project. The owner of every alert is the
repository maintainer (`mvillmow`); escalation is a GitHub issue.

**Response expectation by severity:**

- **critical** (`circuit_breaker_open`): investigate within the same working
  session/day. A stuck-open breaker halts progress for the affected dependency.
- **warning** (`queue_depth_exceeds`, `pipeline_stalled`): review by the next
  automation run; these signal degraded throughput, not a hard stop.

**Escalation path:** file a GitHub issue that references the fired alert `name`
and quotes the corresponding `alert_fired` line from the JSONL event log (see
[Enabling monitoring](#enabling-monitoring)), then follow the runbook linked in
the [Alerts](#alerts) table for that alert.

**Ecosystem collection (Argus).** Argus is the ecosystem's monitoring repo. The
pipeline's `/metrics` endpoint binds loopback only, so an Argus Prometheus
scraper must run on the same host and target `127.0.0.1`:

```yaml
scrape_configs:
  - job_name: hephaestus-automation
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:9123"]   # match --metrics-port
```
