# Runbook: Pipeline Queue Saturation

Use this when the coordinator emits the critical `queue_saturated` alert or a
run ends with resumable work after a completion publication was rejected.
Normal stage backlogs emit `queue_deferred` and drain automatically; they are
not saturation incidents.

## Diagnose

1. Locate the run's JSONL event log under `build/.issue_implementer/` and find
   the `queue_saturated` record. Its `queue`, `capacity`, `source`, and `item`
   fields identify the rejected channel and exact work item:

   ```bash
   rg '"event": "queue_saturated"' build/.issue_implementer/pipeline-events-*.jsonl
   ```

2. Compare the event with the live snapshot or metrics. A stage queue at its
   configured capacity is only a backlog; `queue_saturated` means a completion
   publication or the bounded rejection mailbox could not accept a result.

3. Check the run summary for `RESUMABLE at <stage>`. The coordinator parks the
   exact rejected item before graceful shutdown, and parks all remaining live
   work if the rejection mailbox itself overflows.

## Recover

Re-run the same scoped command after confirming that the event-log directory is
writable and the worker pool is healthy:

```bash
uv run hephaestus-automation-loop --issues <N> --repos <REPO> --loops 1
```

The queue snapshot is in memory only. Recovery reconstructs admission from
GitHub labels, PR state, and local worktrees; the same seed is safe to retry.
Use `--dry-run --loops 1 -v` first when the source state needs inspection.

## Journal failure

If a saturation event cannot be appended, the coordinator fails closed with
exit code `1` and does not continue as though the recovery record existed.
Repair the path or disk-full condition before rerunning. Do not delete the
JSONL log until the incident has been attached to the recovery issue.

## See also

- [Automation loop crashed mid-issue](automation-loop-crash.md)
- [Operations runbooks index](index.md)
