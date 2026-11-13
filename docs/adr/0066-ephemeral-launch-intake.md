# ADR-0066: The repository intake is one ephemeral worktree per launch

- Status: Proposed
- Date: 2026-11-13
- Tracks: #3311

## Context

The automation loop kept one permanent per-repository intake worktree below
the intake state root. A receipt recorded its generation, and each run reused
or rebound that fixed path.

The permanent path created cross-run coupling:

- A run could leave durable state that only a later run could clean. A dirty
  or foreign intake blocked every later run until an operator recovered it.
- The loop needed three ownership layers for one path: a run lease, a
  receipt generation, and a rebind protocol. Each layer had its own failure
  modes.
- A crash left the run lease sentinel and a half-proven checkout. The next
  run could not tell a live run from a dead one without operator help.

## Decision

1. Each launch owns one intake worktree at
   `launches/<launch-identity>/worktree` below the intake state root. The
   launch identity is the run identity. Names are unique, so launches never
   contend for one path.
2. A launch prepares its intake once. Preparation is create-only. It has no
   reuse, no rebind, and no fast-forward proof. The fetched remote head pins
   the detached checkout.
3. The launch receipt is schema version 3. It adds the launch identity. The
   generation is always 1. The durable `intake-registration.json` file keeps
   the launch-independent ownership key and names the active launch. The
   receipt extends that key with the launch identity.
4. One nonblocking exclusive `owner.lock` per launch directory proves live
   ownership. A duplicate launch identity fails closed. The preparation
   nests the Git metadata lock inside the owner lock.
5. Preparation starts with an abandoned-intake recovery sweep under the
   metadata lock. For each other `launches` entry, a nonblocking owner-lock
   acquisition proves that no live launch owns it. A matched receipt, a clean
   detached checkout, and a registry without registered descendants let the
   sweep remove the leftover worktree and tree. A held owner lock or damaged
   state is preserved with a warning.
6. A failure after `worktree add --detach` and before the atomic writes rolls
   the launch tree back inside the same lock scope. A rollback failure
   preserves the tree and reports the retained path.
7. The coordinator removes all launch intakes after both worker lanes stop
   and the final summary finishes. Each removal re-proves the state before
   Git removes the worktree. A preserved failure is a logged warning with the
   retained path and the runbook name. It never changes the run exit code.
8. A SIGKILL or host failure needs no operator cleanup. The kernel releases
   every lock owner, and the next launch's sweep performs the cleanup.
9. The sweep removes the legacy permanent intake at `state_dir/worktree`.
   The removal runs only when its legacy run lock is acquirable. The clean,
   detached, descendant-free proof must also pass. Durable state below it is
   migrated first.

## Consequences

- One launch owns one worktree for one run. A second launch prepares a second
  worktree. Concurrent launches for one repository are safe.
- The launch tree is disposable. A run can remove its whole intake without a
  receipt rewrite, a rebind, or a descendant merge.
- The per-item implementation and review worktrees stay outside the launch
  tree at `build/.worktrees`. Cleanup cannot remove unfinished work.
- `intake_worker_base` answers from the durable registration after the owning
  launch cleanup. The worker base path stays valid for the run.
- Operators see one recovery contract: a retained path, one warning, and one
  runbook. See `docs/runbooks/corrupted-worktree.md`.
- Rollback of this decision restores the previous intake module and its
  tests. Launch directories from the new layout are inert data for the old
  code path and are removed manually.
