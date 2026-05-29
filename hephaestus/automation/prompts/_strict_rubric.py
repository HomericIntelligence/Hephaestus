"""Strict review rubric building blocks.

Composes the grading scale, anti-inflation rules, per-stage dimensions, and
the seven graded software-engineering principles. Per-stage rubrics
(``_PLAN_STRICT_RUBRIC``, ``_PLAN_LOOP_STRICT_RUBRIC``,
``_IMPL_LOOP_STRICT_RUBRIC``, ``_PR_STRICT_RUBRIC``) are assembled from these
shared blocks.
"""

# Rubric block embedded into every loop-review prompt. Mirrors the philosophy
# of the `review-pr-strict` skill at:
#   ~/.claude/plugins/marketplaces/ProjectHephaestus/skills/review-pr-strict/SKILL.md
# Embedded inline so the spawned reviewer process does not depend on the plugin
# being autoloaded — works in any environment that has `claude --print`.
_STRICT_REVIEW_RUBRIC = """
You are a ruthlessly thorough technical reviewer. Apply this rubric per the
`review-pr-strict` skill (rubric summarized below — refer to the full skill at
`~/.claude/plugins/marketplaces/ProjectHephaestus/skills/review-pr-strict/SKILL.md`
if available):

GRADING (every dimension starts at F; A must be EARNED with concrete evidence):
- A  (93-100%) Exemplary, RARE
- B  (80-89%)  Solid with notable gaps
- C  (70-79%)  Mediocre / multiple gaps
- D  (60-69%)  Poor / fundamental practices missing
- F  (<60%)    Failing / misaligned / dangerous

ANTI-INFLATION RULES (mandatory):
- DEFAULT IS F. Find concrete evidence to justify ANY upgrade.
- A requires ZERO critical or major findings.
- B requires ZERO critical findings and ≤1 major finding.
- "It looks done" is NOT sufficient.
- Do NOT give credit for plans, TODOs, or future follow-up issues — grade what
  THIS artifact delivers right now.
- Do NOT round up.

EVALUATE THESE DIMENSIONS:
1. Alignment with the issue's stated requirements
2. Completeness — does the artifact cover every acceptance criterion?
3. Correctness — file paths, function names, API calls actually exist
4. Risk / safety — no destructive ops, irreversible decisions, security holes
5. Scope discipline — KISS / YAGNI; no speculative work
6. Verification plan — concrete steps the reviewer can run to confirm
"""

# Verdict contract for the strict loop reviewers (plan-loop + impl-loop). The
# fenced ``Grade:`` / ``Verdict: <GO|NOGO>`` block below is parsed by
# ``hephaestus.automation.claude_invoke.parse_review_verdict`` (``_VERDICT_RE``
# matches ``GO|NO-GO`` case-insensitively). Do NOT change the GO/NOGO tokens or
# the ``Grade:`` line here without updating that parser in lockstep.
#
# Fail-safe on a missing verdict: when a reviewer omits the verdict line,
# ``parse_review_verdict`` returns AMBIGUOUS, which every gate treats as NOGO →
# the loop iterates / the implementer defers. So a malformed review never
# silently passes. The whole pipeline (plan review + PR review) now speaks the
# single ``Verdict: GO|NOGO`` vocabulary parsed here; the strengthened wording
# below makes the verdict line unambiguous so the AMBIGUOUS fallback is rare.
_STRICT_REVIEW_OUTPUT_FORMAT = """
**Required output format (verdict contract — MANDATORY):**

End your response with EXACTLY these two lines, in this order, each on its own
line, and emit NOTHING after them (no trailing prose, no closing remarks):

```
Grade: <A|B|C|D|F>
Verdict: <GO|NOGO>
```

The `Verdict:` line MUST be present and MUST read either `Verdict: GO` or
`Verdict: NOGO`. Omitting it, or writing any other token, is a
CONTRACT VIOLATION (do not rely on a reader inferring the verdict). GO is
reserved for cases where you are CONFIDENT the artifact is ready as-is. Any
major finding, missing dimension coverage, or "needs another iteration"
critique → Verdict: NOGO. When in doubt, NOGO.
"""


# ---------------------------------------------------------------------------
# Shared rubric building blocks consumed by the per-stage strict-simplify
# review prompts. These constants are the single source of truth for the
# grading scale, anti-inflation rules, and the seven graded software-
# engineering principles from CLAUDE.md `## Key Development Principles`.
#
# They are intentionally additive: the existing `_STRICT_REVIEW_RUBRIC` and
# `_STRICT_REVIEW_OUTPUT_FORMAT` constants above are preserved for backward
# compatibility while sub-issues #578-#581 migrate each review site over to
# stage-tailored rubrics that compose these blocks.
# ---------------------------------------------------------------------------

_PR_STRICT_RUBRIC_DIMENSIONS = """
**PR-review-specific graded dimensions (each starts at F; promote only on
concrete evidence from the artifacts above):**

D1 — Policy compliance (HIGHEST PRIORITY / NOGO gate).
    The three mandatory gates below (Closes #N / auto-merge / signed
    commits) are graded as a single dimension. ANY violation forces an
    overall NOGO verdict, regardless of every other dimension's grade.
    Policy compliance is NEVER weighed against code quality — it is a
    hard precondition.

D2 — Diff review of CHANGED lines only.
    Restrict findings to lines actually modified in the PR diff above.
    Do NOT comment on pre-existing code outside the diff hunks, even if
    it has issues — that is scope-bleed and a finding against the
    reviewer, not the PR. If a changed line depends on unchanged code,
    cite the changed line and reference (not critique) the dependency.

D3 — Inline-comment quality.
    Every inline comment MUST be actionable and specific. Reject filler
    like "consider …", "you might want to …", "maybe …", or vague
    style nags. Each comment must name the concrete defect, the
    expected behavior, and (where non-obvious) a suggested fix or
    citation. Comments that fail this bar must be omitted, not
    softened.

D4 — CI failure analysis (only when CI Status block is non-empty).
    When the CI Status block reports failures, the review MUST identify
    the failing job(s), quote the relevant error signal, and tie each
    failure to a diff hunk (or note "unrelated to diff" with evidence).
    Silently ignoring red CI is a major finding against the review.
"""


_STRICT_GRADING_AND_ANTI_INFLATION = """
GRADING (every dimension starts at F; A must be EARNED with concrete evidence):
- A  (93-100%) Exemplary. ZERO critical/major findings; ≤2 minor. RARE.
- B  (80-89%)  Solid. ZERO critical findings, ≤1 major.
- C  (70-79%)  Mediocre. Multiple gaps that should be prioritized.
- D  (60-69%)  Poor. Fundamental practices missing or broken.
- F  (<60%)    Failing / misaligned / dangerous.

ANTI-INFLATION RULES (MANDATORY):
- DEFAULT IS F. Find concrete evidence to justify ANY upgrade.
- "It looks done" is NOT sufficient.
- Do NOT give credit for plans, TODOs, or follow-up issues — grade what THIS
  artifact delivers right now.
- Do NOT round up. 74% is C, not C+ or B-.
- If you catch yourself wanting to give B+ or higher, re-examine whether you
  verified EVERY dimension or skimmed.
"""


_SEVEN_PRINCIPLES_DIMENSIONS = """
**Software-engineering principles (graded — each can DROP a verdict, not just keep it):**

P1 — KISS — Keep It Simple Stupid.
    Is this the simplest solution that meets the requirement? Flag any
    abstraction layer, generic interface, configuration knob, or
    indirection without a current concrete consumer. Robust ≠ defensive
    cruft. A robust simple solution beats a defensive complex one.

P2 — YAGNI — You Ain't Gonna Need It.
    Every diff hunk / planned-change must map to a stated requirement
    in THIS issue. Flag scope creep, opportunistic refactors mixed in,
    or "while we're here" additions. Features built for hypothetical
    future requirements are findings.

P3 — TDD — Test Driven Development.
    Do tests drive the implementation? Look for test-first evidence:
    tests that define behavior contracts, high coverage of edge cases,
    and tests that landed alongside (not after) the code. For plan
    reviews: does the plan name the tests that will define each
    acceptance criterion? For impl reviews: does the diff include tests
    proportional to the production code?

P4 — DRY — Don't Repeat Yourself.
    If two near-identical code/text blocks exist, prefer extracting a
    helper — UNLESS the extraction would itself add complexity. Flag
    BOTH "left duplicated where it should be DRYed" AND "DRYed
    prematurely into an over-general helper". Cite specific
    duplications with file:line.

P5 — SOLID — five sub-principles, grade each that applies:
    - SRP (Single Responsibility): each module/class/function has ONE
      reason to change.
    - OCP (Open-Closed): open for extension, closed for modification.
    - LSP (Liskov Substitution): subtypes substitutable for base types.
    - ISP (Interface Segregation): no client forced to depend on
      methods it doesn't use.
    - DIP (Dependency Inversion): high-level depends on abstractions,
      not concretions.
    Flag the specific sub-principle violated; vague "SOLID violation"
    is insufficient.

P6 — Modularity — develop independent modules through well-defined
    interfaces. Evaluate coupling, cohesion, and whether module
    boundaries align with domain boundaries. Flag implicit coupling
    (shared globals, unexported state, hidden initialization order).

P7 — POLA — Principle Of Least Astonishment.
    Create intuitive and predictable interfaces. Flag surprising
    defaults, inconsistent naming, non-obvious side effects, silent
    failures, or behavior that contradicts the docstring/name.

**Verdict floor (mandatory):** if ANY of P1–P7 reveals a critical or
major finding, the verdict CANNOT be GO even if every other
dimension scores A. Reviewer must explicitly downgrade and cite the
offending principle by name (e.g. "Verdict: NOGO — P2/YAGNI: diff adds
a config flag with no current consumer; P7/POLA: new flag's default
inverts the existing convention.").
"""


# ---------------------------------------------------------------------------
# Per-stage strict rubric: PLAN REVIEW (standalone Phase-2 reviewer)
#
# Composes the shared strict-grading + anti-inflation rules with plan-stage
# dimensions and the seven software-engineering principles. Injected into
# PLAN_REVIEW_PROMPT so the standalone plan reviewer grades plans against
# the same strict rubric the loop reviewer uses.
# ---------------------------------------------------------------------------

_PLAN_STRICT_RUBRIC = (
    _STRICT_GRADING_AND_ANTI_INFLATION
    + """
Stage-specific dimensions for plan review:

- Requirements alignment: does the plan map every acceptance criterion in
  the issue to a concrete step? Flag any criterion left unaddressed or
  vaguely waved at.
- Plan completeness: are setup, implementation, test, and rollback steps
  all named? Flag missing test plan, missing verification, or implicit
  "and then it works" gaps.
- Concreteness (file paths exist): does the plan name real files,
  functions, and module paths that exist (or will be created with stated
  paths)? Flag hand-wavy "update the relevant module" wording.
- Risk surface: does the plan avoid destructive operations, irreversible
  decisions, and changes outside the issue's stated scope? Flag any
  step that mutates shared state, deletes data, or touches unrelated
  subsystems without justification.
- Verification plan: are the verification steps concrete enough for the
  implementer to copy-paste-run? Flag plans that say "run the tests"
  without naming which tests or "check that it works" without a check.
- Stage handoff: does the implementer receive everything they need
  (file paths, function signatures, test names, commands) to execute
  the plan without re-deriving it? Flag plans that defer key decisions
  to the implementer.
"""
    + _SEVEN_PRINCIPLES_DIMENSIONS
)


# ---------------------------------------------------------------------------
# Per-stage strict rubric: PLAN-LOOP REVIEW (planner's iterative R0/R1/R2)
#
# Same dimensional shape as _PLAN_STRICT_RUBRIC plus the R1+ "addressed-
# not-just-acknowledged" guard. Wired into PLAN_LOOP_REVIEW_PROMPT.
# ---------------------------------------------------------------------------

_PLAN_LOOP_STRICT_RUBRIC = (
    _STRICT_GRADING_AND_ANTI_INFLATION
    + """
**Stage-specific dimensions for plan-loop review:**

1. Requirements alignment — every acceptance criterion in the issue is named
   and addressed by a concrete plan step. Flag silently dropped criteria,
   reinterpretations that narrow scope, or steps that target unrelated work.
2. Plan completeness — the plan covers design, implementation, tests, and
   verification. Flag missing test strategy, missing rollback considerations,
   or hand-waving over hard parts ("then we wire it up").
3. Concreteness — steps name specific files, functions, classes, and
   interfaces by path. Flag vague verbs ("refactor X", "improve Y") with no
   concrete target.
4. Risk surface — destructive operations, schema changes, irreversible
   migrations, and security-sensitive areas are explicitly called out with
   mitigations. Flag risk items the plan ignores or treats as routine.
5. Verification plan — every acceptance criterion has a concrete check
   (command, test name, manual step) the reviewer can run. Flag "we will
   add tests" without naming them.
6. Stage handoff — the plan produces artifacts the implementer can act on
   without re-deriving design decisions. Flag plans that leave key choices
   ("decide framework X later") to the implementer.

**On R1+ (re-review iterations)**: verify previous-iteration's findings were
actually addressed in the new artifact, not just acknowledged or commented
on. A plan that adds a "we will address this" sentence without changing the
plan steps is NOT a fix — flag it as unresolved and downgrade accordingly.
"""
    + _SEVEN_PRINCIPLES_DIMENSIONS
)


# ---------------------------------------------------------------------------
# Per-stage strict rubric: IMPL-LOOP REVIEW (implementer's iterative R0/R1/R2)
#
# Composes the shared strict-grading + anti-inflation rules with impl-stage
# dimensions (graded on the diff, not the plan) and the seven software-
# engineering principles. Wired into IMPL_LOOP_REVIEW_PROMPT so the loop
# reviewer judges the implementer's diff against a strict, stage-tailored
# rubric instead of the legacy generic one.
# ---------------------------------------------------------------------------

_IMPL_LOOP_STRICT_RUBRIC = (
    _STRICT_GRADING_AND_ANTI_INFLATION
    + """
**Stage-specific dimensions for impl-loop review:**

1. Diff fidelity to plan — every hunk in the diff maps to a step the plan
   named. Flag work that drifts from the plan (renamed targets, new
   abstractions, "while I was there" cleanups) without an explicit
   justification. Plan said X, diff did Y is a finding even if Y looks
   reasonable in isolation.
2. Code correctness — file paths, function names, class names, and imports
   referenced by the diff all resolve. Symbols introduced by the diff are
   spelled consistently between definition and call site. Flag dangling
   imports, typos in identifiers, and references to files/functions that
   the diff did not actually create.
3. Test coverage of the diff (P3/TDD has particular bite here) — the diff
   MUST include tests proportional to the production code. Net-new public
   functions need unit tests covering happy path AND at least one error
   path; bugfixes need a regression test that fails without the fix. A
   diff that adds production code with no corresponding test changes is a
   MAJOR finding by default.
4. No regression of unrelated tests — the diff does not delete, weaken,
   `xfail`, `skip`, or `monkeypatch`-around existing tests to make the
   build green. Flag any test deletion or assertion-loosening that the
   issue did not explicitly request.
5. Safety — no destructive operations slipped in (no `rm -rf`, no
   schema/data migrations without rollback notes, no unscoped `sudo`,
   no committed secrets or tokens, no `--no-verify` / `|| true`
   silencers). Flag credentials in env-var defaults, hard-coded
   API keys, or signing-key material.
6. Diff scope — every hunk maps to a stated requirement in the issue
   (YAGNI applied at the diff level). Flag opportunistic refactors,
   formatting churn in untouched files, dependency bumps that weren't
   asked for, or new config knobs without a current consumer.

**On R1+ (re-review iterations)**: verify previous-iteration's findings
were actually addressed in THIS diff, not just acknowledged in commit
messages or comments. A diff that adds a "TODO: fix this" without
changing the offending code is NOT a fix — flag it as unresolved and
downgrade accordingly.
"""
    + _SEVEN_PRINCIPLES_DIMENSIONS
)


# ---------------------------------------------------------------------------
# Per-stage strict rubric: PR REVIEW
#
# Composite rubric injected into PR_REVIEW_ANALYSIS_PROMPT (site 4 / #581).
# Order: strict grading scale → PR-specific dimensions (D1 policy is the
# NOGO gate) → seven software-engineering principles. The existing
# policy-checks block in PR_REVIEW_ANALYSIS_PROMPT remains the
# authoritative description of the three gates referenced by D1, and the
# trailing JSON output format remains byte-exact for `_parse_json_block`.
# ---------------------------------------------------------------------------
_PR_STRICT_RUBRIC = (
    _STRICT_GRADING_AND_ANTI_INFLATION
    + "\n"
    + _PR_STRICT_RUBRIC_DIMENSIONS
    + "\n"
    + _SEVEN_PRINCIPLES_DIMENSIONS
)


# ---------------------------------------------------------------------------
# Final-iteration full-sweep suffix (appended on R2 by both PLAN_LOOP and
# IMPL_LOOP review prompts). Adds cross-cutting checks above and beyond
# the per-stage rubric — security review, dependency-graph impact, doc
# drift. Issue-scoped, not repo-scoped. Site 3 (#580) reuses this constant.
# ---------------------------------------------------------------------------
_FULL_SWEEP_SUFFIX = """
## Final-iteration Full-Sweep (R2 only)

This is the FINAL review iteration. In addition to the per-dimension rubric
above, perform the following cross-cutting sweeps. Keep each sweep scoped to
THIS issue's plan — do NOT broaden into a repo-wide audit. Flag findings
inside the existing Grade/Verdict; do NOT emit a separate verdict line.

### S1 — Cross-cutting security review of this plan

Read every plan step that touches:
- external input (HTTP, CLI args, env vars, file uploads, message payloads),
- subprocess invocation or shell commands,
- credentials, tokens, signing keys, or secret material,
- unsafe deserialization (binary-object loaders, YAML.load, JSON with custom
  decoders),
- file-system writes outside the build/ directory,
- network egress to new endpoints.

For each such step, verify the plan names a concrete mitigation
(parameterized commands, allow-listed inputs, secret-via-env, schema
validation, path-traversal guard, TLS verification). Missing mitigation on a
security-relevant step is a MAJOR finding. Vague mitigation ("we will
validate inputs") without naming the validator is a MAJOR finding.

### S2 — Dependency-graph impact analysis

Identify every shared module the plan modifies (anything under
`hephaestus/` consumed by ≥2 callers, plus any public API exported from a
package `__init__.py`). For each:
- Does the plan acknowledge downstream callers?
- Does the plan preserve the existing call signature or schedule a
  coordinated update?
- Does the plan add a new shared utility without checking whether an
  existing one already covers the case (DRY)?

A shared-module change with no caller-coordination note is a MAJOR finding.
A shared-module change that breaks an existing signature without a
deprecation path is CRITICAL.

### S3 — Documentation-drift check

For every new concept the plan introduces — new module, new public
function, new CLI flag, new config knob, new env var, new file layout —
verify the plan also names a documentation update (README, docstring,
docs/ markdown, or skill). A new public surface with no doc update is a
MAJOR finding. A new config knob with no doc update is a MAJOR finding.
A purely-internal refactor that touches no public surface needs no doc
update — do not flag.

### Sweep output

After completing S1/S2/S3 above, fold any new findings into the existing
per-dimension scoring. The Grade and Verdict lines remain the SINGLE
output verdict — do not emit duplicate verdicts for the sweep.
"""
