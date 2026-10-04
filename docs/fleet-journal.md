# Fleet worker journal

`WorkerJournal` keeps private execution observations and command receipts.
It does not admit tasks, grant a replacement lease, or change Agamemnon state.
The journal keeps the existing JSONL format and process writer flock.

## Shared use in one process

One reentrant thread lock serializes journal operations through the same
Python object. An append holds the lock through its size check, write, flush,
file synchronization, and in-memory update. `begin()` holds it through the
command identity check and the retained intent write. A second opened journal
still fails while the process writer flock is held.

Use `snapshot()` to obtain a deep, caller-owned copy of projected state. It
includes records, command maps, sessions, events, runtime fields, generation,
drain state, and the `write_uncertain` and `closed` flags. Changes to the copy
do not affect the journal. Later writes do not alter a previously returned
copy. The old public state attributes remain mutable aliases for compatibility.
They do not become safe concurrent read or write interfaces in this change.

Use `transaction()` for a short compound read and write:

```python
with journal.transaction():
    state = journal.snapshot()
    sequence = len(state["events"]) + 1
    journal.append("event", {"seq": sequence, "kind": "example"})
```

This context is a lock scope, not a durable multi-record transaction. It does
not roll back an acknowledged append when later caller code raises. Do not
hold it across a provider request, HTTP operation, an await, or a pool join.
The synchronous storage methods do not provide an asynchronous deadline or
preempt stalled disk I/O.

`require_writable()` checks the current open writer health under that same
lock. It writes no record and grants no admission. A saved intent does not
permit a caller to skip this check before another effect. The result is not a
transferable permit and cannot predict a later concurrent storage error.

## Uncertain effects

A JSON or size rejection before a write leaves the journal usable. A failed
write, short write, flush, file synchronization, or in-memory application
fences the open journal. It keeps the process writer lock and rejects every
later mutator, including a cached `begin()` and a new transaction.

Read-only snapshots remain available after an uncertain effect. Their
`write_uncertain` flag is true. The projected state can contain only the
confirmed prefix, or a partially applied observation if the in-memory update
failed. A snapshot is not proof that all actual bytes were durable. Retain
the actual journal and workspace for reconciliation; do not retry the write
or infer task completion from the snapshot.

The fence is local to the open object. Closing and reopening the file permits
inspection of actual retained bytes. It does not clear uncertain execution
authority, authorize a new provider action, or replace the controller's
reconciliation decision. Existing uncompleted command intents still return
an unknown outcome on replay.

## Closing resources

`close()` waits for active journal storage operations and is idempotent. It
does not wait for an entire provider request, SDK coroutine, or worker-pool
job. The runtime owner must wait for those borrowers before it closes the
journal.

If closing the data stream raises, the journal keeps the process writer lock
and sets its uncertain-write fence. Read-only observations remain available.
A later explicit close can finish resource release. A closed journal rejects
mutations and preserves its final snapshot for inspection.

## Caller integration still required

This primitive change does not migrate FleetWorker or FleetBuildOwner.
FleetWorker's event-sequence read and append need the short transaction when
shared callers are activated. FleetBuildOwner must replace direct record-list
reads with snapshots and check journal health even when its intent was already
saved. Its existing build-owner-only poison flag is insufficient after a
worker-side storage failure.

FleetWorker also attempts to append a provider cleanup observation during
close. A poisoned journal will reject that append. The later runtime change
must propagate that storage failure and must not claim that the cleanup
observation was retained. A provider's actual cleanup and durable recording of
that cleanup remain distinct facts.

No Fleet runtime, build submission, source lease, result handoff, or stage
activation is supplied by this primitive alone.
