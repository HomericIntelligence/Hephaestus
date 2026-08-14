# ADR-0027: Durable plan-review conversations

- Status: Accepted
- Date: 2026-08-14
- Tracks: #2766

## Context

Plan review previously submitted a one-shot reviewer job for every revision.
The issue comment journal retained the latest plan and review, but not the
provider conversation identity or the complete review history. Retries and
runner restarts could therefore create a fresh reviewer that repeated or
reframed findings instead of checking whether an amendment addressed them.

## Decision

One explicit planning cycle owns one provider-neutral reviewer conversation.
Persist a versioned journal under `build/.automation-state/plan-review` keyed
by repository, issue, and an opaque cycle UUID. The record binds the provider,
model/configuration, canonical checkout, provider session id, round, plan
revision/fingerprint, state, and digest-checked review/amendment artifacts.

The first review uses `START_NEW`; every later review uses
`RESUME_REQUIRED`. Persist the provider identity before parsing model output.
Every prompt includes the latest plan identity and the complete verified
transcript. Retries and process restarts recover the active record. A new
issue, a new planning cycle, or the explicit issue-scoped
`--reset-plan-review-session` option creates a new identity.

Missing, corrupt, mismatched, or provider-unresumable state is terminal for
autonomous review. Persist `recovery-required`, expose
`review-session-lost`, and apply the existing plan-blocked recovery latch.
Never create a replacement conversation implicitly. The journal does not
change worktree or sandbox isolation.

## Alternatives considered

- Reconstruct context from issue comments. Rejected because the comments do
  not preserve the provider's complete rationale or conversation state.
- Start a fresh reviewer after resume failure. Rejected because it silently
  changes the reviewer lineage and can approve against incomplete history.
- Reuse one reviewer per role or repository. Rejected because separate issues
  and planning cycles must not share transcript state.

## Consequences

Iterative review has stable identity across amendments, retries, and runner
restarts. Operators can inspect cycle/session/round/revision in logs and
status output and must explicitly reset a lost conversation before work can
continue. Transcript integrity and conservative failure handling add bounded
local state and can trade availability for review continuity.
