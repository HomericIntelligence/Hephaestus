# ADR-0055: Conditional manual rebases

- Status: Accepted
- Date: 2026-09-18
- Tracks: #3291

## Context

ADR-0053 requires a signed final rebase before manual publication. This
changes the candidate head even when the target advance has no relevant
change. It also requires repeated checks without a source-related need.

## Decision

Supersede only the unconditional manual rebase requirement in ADR-0053.
Rebase for an actual conflict, a necessary dependency, or an explicit request.
Record the worktree start commit as the local review base. Target movement
alone does not change that base or the reviewed candidate.

Keep isolated worktrees, signed commits, exactly one matching final DCO
trailer, focused checks, and clean source-revision records. After a source
change, run the affected checks on the resulting head. Preserve the original
revision and result of every historical execution. Use historical evidence
only with an explicit compatibility basis for the relevant inputs. Never
report it as a new current-head run.

Source review, execution evidence, and merge admission remain separate.
Required CI and current branch protection still apply to the merge candidate.
A pending required check cannot authorize a merge. This decision changes no
production action, validation runtime, isolation control, or merge policy.

The automation loop retains ADR-0048. The related Comet evidence audit is
recorded in [the audit report](../workflow-evidence-audit.md).

## Alternatives considered

Keep routine final rebases: rejected because target movement alone does not
show a conflict or dependency that requires a different candidate.

Reuse all earlier test results: rejected because changed source or controls
can invalidate evidence. A compatibility claim needs an explicit basis.

## Consequences

Manual publication does not rewrite a branch solely to match a newer target.
Authors still resolve actual integration needs and verify the resulting head.
Required CI remains a separate merge condition. ADR-0053 continues to define
focused local checks and CI-owned full validation.
