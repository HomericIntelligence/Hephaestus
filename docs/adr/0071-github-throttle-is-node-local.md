# ADR-0071: The GitHub throttle is node local

- Status: Accepted
- Date: 2026-11-23
- Tracks: #3689, #1519, #1883

## Context

The global GitHub throttle keeps a token bucket in a JSON file. The old code
put this file below `TMPDIR` and held `fcntl.flock` on it. A launcher can set
`TMPDIR` to a shared filesystem. A file lock on a shared filesystem can wait
without a bound inside the storage client. The shared file also coordinated
different machines, but the budget is for one machine.

Issue #1519 moved runtime state below `TMPDIR` because a user runtime
directory was not writable. Issue #1883 covered a different lock. Neither
issue removed this shared-filesystem lock path.

## Decision

1. The global throttle budget has one-user machine scope. `GLOBAL_THROTTLE_SCOPE`
   is `"machine"`. The state file coordinates processes for one operating-system
   user on one machine. It does not coordinate separate hosts or different
   operating-system users.
2. The state root comes from a node-local runtime directory. On Linux, the
   order is `XDG_RUNTIME_DIR`, `/run/user/<uid>`, then `/dev/shm`. On macOS,
   the root is `os.confstr(_CS_DARWIN_USER_TEMP_DIR)`, where the constant is
   the integer `65537` from `<unistd.h>`. The code does not read `TMPDIR`,
   `TMP`, or `TEMP`.
3. The code probes each candidate and keeps the first writable candidate. A
   per-user `hephaestus-<uid>` directory keeps the state private. If no
   candidate is usable, the throttle stops with an error that names the
   recovery flag.
4. `--gh-throttle-dir PATH` sets an explicit absolute parent directory. This
   override is for a host with no usable runtime root. The operator must select
   a node-local directory. The code cannot prove that an arbitrary path is
   local.
5. The process lock is a nonblocking `fcntl.flock` poll loop. Every caller uses
   the loop. The wait obeys the existing deadline and cancellation. A file lock
   does not wait inside the storage client. A caller with no deadline can still
   wait while another process holds the lock. The lock stays on the
   state-file inode, so the code does not publish the state with `os.replace`.
6. The existing private-directory, ownership, symlink, persisted-state,
   cancellation, and deadline behavior stays.

## Alternatives considered

- Keep `TMPDIR` and use a blocking lock. This keeps the shared-filesystem lock
  hazard and the unbounded wait.
- Use Redis or a database. The repository has no such client. A network
  service adds a dependency, credentials, and a new failure domain. The issue
  permits a machine-local budget.
- Use an abstract Unix socket mutex. It is kernel-local, but Linux-only. macOS
  needs a second mechanism.
- Detect network filesystems by mount type. The blocklist is incomplete and
  platform-specific. The operating-system runtime directories give the
  locality contract with less code.

## Consequences

- The throttle is correct for one machine. A multi-machine fleet can send up
  to one budget per machine. An operator must divide `--gh-global-rate` across
  the machines.
- The old state file below `TMPDIR` stays on disk and is ignored. The first
  call on the new root starts with a full bucket. While old and new processes
  run together, the account can use up to two machine budgets. The refill and
  burst contract does not change.
- A container with a private `/dev/shm` has a per-container budget. A host
  that needs one budget must bind-mount a node-local directory and pass it
  with `--gh-throttle-dir`.
- An operator can still select a shared path with `--gh-throttle-dir`. The
  help text and this ADR state that this use is unsupported.

## Rollback

Revert the change. The old code reads the old `TMPDIR` path again. The new path
is ignored. No state is deleted. If the revert occurs after this ADR is
accepted, mark the ADR superseded. Do not rewrite its history.
