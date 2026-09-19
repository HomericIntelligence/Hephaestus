# ADR-0056: Explicit host capabilities and durable operation recovery

- Status: Draft
- Date: 2026-09-19
- Tracks: #2903

## Context

A host can lack the quota or signing setup needed for an operation. This is not
evidence that the source is incorrect. Source review does not execute tests and
does not need a quota image. Capability-dependent execution still needs its
existing isolation checks. CI/CD supplies full-suite evidence separately.

A signed rebase can finish before validation or publication fails. The resulting
local head can differ from the remote head. A process restart must not repeat the
rebase or replace a competing remote change. First publication has a different
owner and requires proof that the remote branch is absent.

## Decision

Keep frozen capability requests, results, and protocols in the pipeline contract
module. Keep platform discovery, process calls, storage, and caches in the
external automation adapter. The coordinator supplies providers to workers.
Stages use closed jobs and consume only results for their current request.

Quota checks use the existing macOS or configured Linux isolation owner. Missing
providers block execution. Each result binds the request, verified source head,
canonical root, device, backend, and execution boundary. Store each receipt in a
private namespace with strict validation, atomic replacement, and readback. If
storage fails, return a distinct storage diagnostic and retain the original
capability failure. A receipt is not a test result or review approval.

Cache only quota probe outcomes within one process and execution boundary. Each
cache hit needs a new request-bound receipt. A new process probes again; a stored
receipt cannot replace current capability or source checks. Keep per-execution
isolation lifecycle checks. Preserve primary and cleanup errors separately.

Signing providers call the existing production validator. Keep its identity,
key, tool, and environment checks. Test fixtures can supply explicit providers;
there is no unsigned production fallback.

Capability failure enters the existing recoverable blocked state. It does not
change source verdicts, consume remediation budget, launch a writer, or grant
merge authority. Retain durable evidence and clear transient request ownership
on restart. A fresh request can retry the unchanged source after host recovery.

### Rebase recovery

The existing worker owns mutation and publication. A separate protected storage
owner retains one operation identity. Bind its repository, destination, branch,
source owner and generation, original input and remote lease, base, policy, and
phase. Persist and read back intent before mutation. After success, independently
read the resulting head and tree and persist pending validation before execution.

Fresh discovery returns an untrusted candidate and the source manager's current
binding. Under the existing repository, Git-common, and source locks, validate
the candidate, clean source, destination, original lease, policy, ancestry, and
signed metadata again. Resume repeats checks, not the rebase. It cannot launch a
writer or recover approval from a stored record.

Known conflicts retain the original intent. Continuation still needs independent
paused-source and conflict admission. Intent alone cannot reconstruct it. A
confirmed abort requires checked Git completion, no remaining rebase state,
clean restored source, exact original head and tree, and unchanged ownership.
Persist a terminal aborted record before reporting the policy outcome. An
aborted record grants no authority for another attempt.

Persist publication intent before an exact-lease push. An uncertain result needs
a fresh authenticated remote observation. The original remote head permits a
new fully validated attempt. The intended result permits reconciliation only
with matching publication intent. Any other head, failed observation, or invalid
record blocks. Complete the operation only after the existing owner confirms
publication or local-only completion. Local no-PR rebase still publishes nothing.

### First publication

First publication belongs to `commit_push`, not rebase. After the existing owner
creates or identifies the signed implementation commit, retain a distinct
operation with its exact head, tree, source owner, approved scope, base, and
absent-remote expectation. Write and read back this intent before push.

Recovery obtains current approved paths and a fresh authenticated main snapshot.
Require successful object and ancestry checks. The retained start must be an
ancestor of the retained head. Find the unique merge base of authenticated main
and that head. With rename detection disabled, check the complete merge-base
diff and retained-start diff to the head. Require both to fit current scope.
Do not require the retained start to remain on main. Existing start evidence
must agree; missing evidence cannot replace the independent merge-base check.

Confirmed remote absence permits an absent-only conditional push with normal
hooks. The exact intended remote head permits reconciliation only with matching
durable intent. A competing head or failed observation blocks. Do not rerun the
writer, commit, or rebase. Recheck current gates before the existing callback can
advance to PR creation. Completed records permit a fresh request-bound callback,
not another publication or completion-record mutation. Reject stale and duplicate
callbacks without advancing another request. A crash before valid intent needs
operator recovery.

### Storage and authority

Use separate protected namespaces for rebase and first-publication records.
Validate ownership, permissions, schemas, regular single-link files, and path
identity. Use descriptor-relative no-follow access and bounded reads. Preserve
invalid or ambiguous evidence. Record locks are short: release them before Git,
source-lock acquisition, or network operations. Keep deadlines and cancellation.

This decision adds no queue, general journal, backend registry, rebase trigger,
review approval, or merge permission. ADR-0048 still controls automation rebase
triggers. Existing source-review and exact-head CI/CD merge gates remain separate.

## Alternatives considered

- Treating setup failure as a source defect gives the wrong verdict and can
  launch an unnecessary writer.
- Replaying a signed operation after restart can change history twice.
- Using source ownership or an initial-start record as publication authority
  omits the original remote lease and operation phase.
- Publishing an absent branch from rebase changes local-only behavior and moves
  publication away from its existing owner.
- Trusting a retained scope base alone can hide unapproved PR changes.
- Reusing durable quota receipts across processes omits fresh host checks.

## Consequences

Some failures require operator action instead of automatic replay. Records and
failed cleanup artifacts remain available for inspection. Recovery repeats
validation and can cost more than normal execution, but it does not repeat the
source mutation. Rollback must retain pending records and prevent unguarded replay.

Tests must use real stage, worker, source-manager, and storage owners with bounded
external seams. Cover new-process retries, real process death, ambiguous windows,
source and remote drift, storage failure, competing attempts, stale callbacks,
and unchanged verdicts and budgets. Focused checks run locally through test-only
delegates. CI/CD owns full-suite execution. Neither evidence source independently
grants review or merge authority.
