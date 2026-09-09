# Learning evidence and recovery

The learning queue accepts post-merge implementation evidence. Plan approval
keeps the plan on the source issue. It does not create a learning intent.
The host rejects old `approved_plan` requests with `plan_only_learning_rejected`.
Existing journal keys and completed records stay unchanged.

## Publication requirements

The host confirms the merged PR, its closing issue, and its merge commit.
It reads the required checks and confirms the PR head again after that read.
At least one required check must exist. Every required check must pass.
Missing merge or check evidence defers learning.

Passing checks do not make PR prose a reusable lesson. The default builder
returns `learning_deferred:candidate_required`. Automatic lesson generation is
not provided. An application can supply a reviewed candidate through the
existing `MnemosyneLearningBuilder` interface.

For a supplied candidate, the host searches the bound skill corpus. A match
must identify one existing entry, and the candidate must update that path.
An ambiguous match or a different path defers publication for review.
The host sets `verification` to `verified-ci` from passing implementation
checks. It adds the checked head, merge commit, and check evidence to the
candidate. It does not assign `production-host` verification.

Before delivery, the candidate must pass the target repository's
`scripts/validate_plugins.py` and Markdown lint configuration. The host must
have `node` and `markdownlint-cli2` on its executable path. Both checks run
without network access in the validation sandbox. A missing tool or failed
check prevents publication. A raw `learn_delivery` payload cannot prove source
evidence and returns `learning_deferred:source_evidence_required`.

## Deferred records

The journal supports the additional `deferred` status. This status is terminal
for the current attempt. It does not report learning success or fail the source
issue. The summary counts deferred auxiliary jobs separately from failed jobs.
Cleanup can complete after a deferred outcome.

Only a host with new candidate evidence can resume a deferred intent. It calls
`LearningJournalStore.resume_deferred` with the SHA-256 fingerprint of that
evidence. The same fingerprint cannot resume the intent again. This operation
retains the journal identity and does not change completed success or failure
records. It does not generate or approve a candidate.

Use the upgraded host for journals that contain deferred records. Older
releases do not recognize this status. No bulk rewrite of existing journals is
required.

## Failure recovery

The journal retains the first error as `first_error` and the latest error as
`error`. A failed validation leaves its candidate in the prepared worktree.
A later attempt cannot delete an unpublished candidate worktree. Inspect and
repair or archive that candidate before another delivery attempt.
