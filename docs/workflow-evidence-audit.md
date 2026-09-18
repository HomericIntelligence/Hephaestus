# Workflow Evidence Audit

## Scope and source

This audit addresses the Comet investigation in
[Hephaestus #3291](https://github.com/HomericIntelligence/Hephaestus/issues/3291).
The inspected source is commit
`3a0770e48a508a910e851ec283dabb92e7c9902b`. Issue and pull-request states below
were read on September 18, 2026. This audit does not change evidence admission.

## Findings

Comet can reject retained execution evidence when the pull-request head does
not change but the target base advances.

- [Stage coverage](../hephaestus/automation/pipeline/stages/pr_review_repository_validation_state.py)
  passes the live `pr_base_sha` to `validation_attempt_coverage()`.
- [Receipt evaluation](../hephaestus/automation/pipeline/repository_validation.py)
  rejects a difference between that base and `plan.reviewed_base` with
  `reviewed_revision_mismatch`. The plan digest includes the original base.
  Receipt matching also requires the original plan ID, head, and base.
- [Comet source and CI admission](../hephaestus/automation/pipeline_github_review_validation.py)
  separates `reviewed_base` from `diff_base_sha` during source inspection.
  However, `comet_ci_identity()` requires the PR base to equal the recorded
  base. It also requires the CI merge commit parents to equal the recorded
  base and head. The retained CI result can therefore describe a different
  integration tree from the current one.

The base comparison can cause unnecessary invalidation. However, removal of
that comparison alone would remove an evidence boundary. The current code
has no explicit compatibility record for a later target base.

## Required compatibility basis

A future reuse change must retain the original execution record. It must not
replace its receipt ID, plan ID, head, or base with current values.

The host must establish an explicit compatibility basis before it uses that
record for a later base. That basis must bind the original evidence and the
new target base. It must verify the unchanged head, selected change inventory,
admitted control profile, selected commands, and relevant inputs for each
check. The existing separation of target base and diff base can support this
work. It is not sufficient proof by itself.

Report reused evidence as historical evidence with a compatibility basis.
Do not report it as a newly executed current-head result. A relevant input
change must invalidate the affected evidence. Missing compatibility proof
must leave an evidence gap.

Keep three decisions separate:

1. Source review evaluates the exact bound source.
2. Execution evidence records what ran, against which inputs, and its result.
3. Merge admission checks the current required CI and other merge gates.

A pending required check cannot authorize merge. Historical compatibility
must not replace required CI, exact-head review, isolation, or production
authorization.

## Existing work and coordination

- [Hephaestus #3217](https://github.com/HomericIntelligence/Hephaestus/issues/3217)
  owns retained rebase-review proof after the base advances. Its scope keeps
  publication-time target-base validation unchanged. Do not duplicate that
  recovery change in the Comet profile.
- [Hephaestus #2623](https://github.com/HomericIntelligence/Hephaestus/issues/2623)
  owns duplicate host full-suite validation and focused correction prompts.
  Reuse that work. This audit does not add another host-validation system.
- [Hephaestus #3218](https://github.com/HomericIntelligence/Hephaestus/issues/3218)
  owns wait behavior for ambiguous non-conflicting fleet-sync states. It does
  not own Comet evidence admission or tidy behavior.
- [Hephaestus #2903](https://github.com/HomericIntelligence/Hephaestus/issues/2903)
  and open [PR #3239](https://github.com/HomericIntelligence/Hephaestus/pull/3239)
  own explicit, recoverable host capabilities. Reuse those boundaries instead
  of adding a competing execution path.
- Open [Athena #266](https://github.com/HomericIntelligence/Athena/issues/266)
  requires source-review GO and NO-GO to be independent of CI/CD. Source
  review must not wait for CI or use CI status to decide its verdict. This
  agrees with the separation above. Coordinate any future Comet reuse change
  with that contract. CI remains a separate merge gate. This audit records
  the inspected issue requirements; it does not claim that Athena #266 is
  complete or that an external coordination message was published.

## Validation limits and future tests

This is a source audit. No Comet workload or validation command was executed.
It establishes the rejection path, not a complete per-check dependency map.
It does not establish that any particular historical CI run is compatible
with a later base.

Before an executable reuse change, add focused failing tests in
[receipt tests](../tests/unit/automation/pipeline/test_repository_validation.py)
and [stage tests](../tests/unit/automation/pipeline/stages/test_pr_review_comet_validation.py).
Cover an unchanged head with an unrelated base advance, a relevant input
change, a changed head, missing compatibility proof, and unchanged original
receipt identity. Keep tests for CI merge-parent and workflow provenance.
Keep pending evidence requests and pending required merge checks closed.
