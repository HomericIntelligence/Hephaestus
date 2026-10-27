# Runbook: Queue Saturation

Use this runbook when the pipeline reports a `queue_saturated` alert, or when
`queue_depth_exceeds` repeats for one stage. Both alerts read the coordinator's
live snapshot. The depth alert compares a ready backlog with a percentage of one
stage queue's bounded capacity. The saturation alert reports a queue that holds
its full occupancy.

`queue_saturated` is critical. `saturated_queues` containing `completion` is a
fatal internal fault. The run stops.

## Inspect the current state

Read the readiness snapshot. The port is the configured `--metrics-port`.

```bash
curl -s 127.0.0.1:<port>/health | jq '{status, queue_depths, queue_capacities, saturated_queues}'
```

Read the alert transitions and the completion fault from the JSONL event log.

```bash
grep -E '"alert_(fired|resolved)"|completion_saturation' <event-log>.jsonl
```

Read the exported gauges.

```bash
curl -s 127.0.0.1:<port>/metrics | grep -E 'hephaestus_pipeline_(queue_depth|alert_active)'
```

## Diagnose

| Observation | Meaning |
| --- | --- |
| One stage queue is at capacity and work still completes | Ordinary backpressure. The stage is slower than its producers. |
| `queue_depth_exceeds` repeats for the same stage | That stage is the bottleneck. Check its job durations before you change capacity. |
| A queue stays at capacity and `stalled_ticks` grows | The stage cannot drain. Inspect its worker completions and its agent job results. |
| `saturated_queues` contains `completion` | A worker pool could not enqueue a completion. This is a fatal internal fault, not load. |

## Recover

1. **Ordinary backpressure:** wait for the drain. Capacity is released when each
   job completes.
2. **Sustained load:** raise `--max-workers` or `--parallel-repos` for the main
   lane. Raise `--learning-queue-capacity` for the learning lane.
3. **One stage is always the bottleneck:** investigate that stage. Do not raise
   capacity to hide a stage that cannot finish its work.
4. **Completion saturation:** do not restart blindly. Preserve the event log,
   then file a GitHub issue that quotes the `completion_saturation` record.

## Related

- [Observability](../observability.md) — the alert table, gauges, and SLOs.
- [Automation loop crashed mid-issue](automation-loop-crash.md) — resume after a
  stopped run.
