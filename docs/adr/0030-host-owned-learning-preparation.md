# ADR-0030: Host-owned learning preparation

- Status: Accepted
- Date: 2026-08-14
- Tracks: #2754

## Context

ADR-0026 moved learning into an auxiliary host lane, but the lane could only
consume a complete `learn_delivery` object. No production component created
that object. Ordinary approved-plan and post-merge intents therefore ended in
the ancillary error `learn delivery payload is required`.

Preparation cannot be delegated to a generic agent provider. Repository
binding, the write allowlist, signed DCO commits, PR creation, and immutable
readback proof are Mnemosyne host responsibilities.

## Decision

`LearningStage` submits a closed semantic `learning_intent`. The host validates
its deterministic key and re-reads the immutable GitHub source: the exact
actor-owned approved plan under exclusive `state:plan-go`, or the exact merged
PR that closes the issue.

The provider-neutral preparation service renders at most one flat
`skills/*.md` artifact, capped at 65,536 bytes. It normalizes and quotes
untrusted source text, prepares a deterministic isolated worktree from the
Mnemosyne binding (or a live retry PR head), and runs the fixed offline
Mnemosyne validator under a restricted environment. Its output is the existing
`LearnDeliveryRequest`; that schema remains the closed compatibility surface
for explicit callers.

`MnemosyneSkillHost` accepts exactly one of `learning_intent` and
`learn_delivery`. It passes the binding receipt explicitly to the delivery
backend and requires the request repository and base branch to equal that
receipt. `LearnDeliveryService` independently verifies the worktree origin
before staging on both create and reuse paths, then retains ownership of the
allowlist, signed DCO commit, push, PR creation/reuse, and head-SHA readback.

## Alternatives considered

- Construct delivery payloads in `LearningStage`. Rejected because the stage
  does not own the Mnemosyne binding or trusted source reads.
- Dispatch an `AgentJob` to write the skill. Rejected because provider policy
  and Pi preflight are unrelated to host learning and would widen authority.
- Change the delivery schema. Rejected because the existing closed request and
  receipt already encode the required Git and GitHub controls.

## Consequences

The complete production learning path can run with every agent-provider entry
point disabled. Preparation failures remain ancillary under ADR-0026, while a
successful intent now reaches a signed, PR-backed Mnemosyne delivery. Explicit
delivery callers remain supported but malformed, ambiguous, or binding-mismatched
requests fail closed before repository mutation.
