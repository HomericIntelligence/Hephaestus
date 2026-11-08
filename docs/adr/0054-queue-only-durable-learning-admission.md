# ADR-0054: Queue-only durable learning admission

- Status: Accepted
- Date: 2026-11-08
- Tracks: #3577

## Context

A session that the queue did not create has no queue-owned source. The learning
lane admits one source: a merged PR that closes an issue and passed its required
checks. `GitHubLearningSourceReader._post_merge` requires the merged state, the
merge commit, and the closing-issue reference
(`hephaestus/automation/mnemosyne_learning_preparation.py:227-269`). It then
requires every required check to pass and reads the PR head again
(`:181-197`). A bare session identifier satisfies none of these conditions. The
host defers such a request with `learning_deferred:source_evidence_required`
(`hephaestus/automation/mnemosyne_skill_host.py:262`).

This record refines ADR-0026 and ADR-0032. It does not replace them. Those
records make the auxiliary lane and the host re-read the unit of admission. This
record states which sources can enter that unit.

## Decision

The queue is the only durable-learning admission lane. A source enters durable
Mnemosyne delivery only through a `LearningIntent` that a queue stage emits,
and the host then confirms the merged PR evidence again. The merged-PR
predicate is the sole admission evidence. An operator assertion cannot replace
it.

This decision governs durable Mnemosyne delivery only. It does not govern the
learning CLI, its `--session-id` and `--agent` surface, or its interactive learn
turn. That work stays with its own scope.

## Alternatives considered

- Session registration. A new `--session-id` record could bind a repository,
  issue, working directory, and workspace to one session. Rejected: no such
  identity satisfies `_post_merge`, so delivery still defers. The record adds a
  second admission path that the delivery boundary rejects, and it lets an
  operator assertion replace the verified merge and check evidence.
- A new `LearningIntentKind` for CLI sessions. Rejected: every intent kind needs
  a `repo`, an `issue`, and merged-PR proof. A CLI session supplies none of them,
  so the new kind adds a branch that always defers.

## Consequences

- An operator path exists, and it has a condition. A PR that a session creates
  is admitted only when its linked issue is in the queue and the PR obtains
  `state:implementation-go` through review
  (`hephaestus/automation/pipeline/seeding.py:288-293`,
  `hephaestus/automation/pipeline/stages/merge_wait.py:274-276`). A PR with no
  linked issue is not admitted. It stays orphaned and fails closed before PR
  review (`hephaestus/automation/pipeline/coordinator_sources.py:762-767`).
- This consequence is verified at commit `ba71fb74` by the PR-seeding route and
  by `_route_merged`. It is not a future promise.
- A CLI-session PR that opens before the learning CLI lands still follows this
  path. That CLI changes nothing here.
- The decision is reversible. It records policy only. A change needs no schema
  change and no journal migration.
