"""Regression guard: each phase module passes the correct AGENT_* constant.

The phase modules split into three categories:

- **Self-agent phases** (`implementer`, `ci_driver`) own one long-lived
  session identity — each passes its dedicated ``AGENT_*`` constant to
  ``invoke_claude_with_session`` so its session UUID is distinct from every
  other phase's AND stable across the stage (resumed, not recreated).
  ``ci_driver`` owns Session 3 (``AGENT_CI_DRIVER``): drive-green polls CI,
  runs its own fix sessions, and captures its own learnings on a transcript
  independent of the implementer.
- **Per-iteration reviewer phases** (`plan_reviewer`, `pr_reviewer`) must run
  as a *fresh* session every review-loop iteration so the reviewer never
  inherits its own prior verdict (the #455/#468/#484 self-review bug). They
  derive their per-iteration agent token via
  ``reviewer_agent(AGENT_*_REVIEWER, iteration)`` rather than a static
  ``agent=AGENT_*_REVIEWER`` kwarg, so the guard asserts that wiring instead.
- **Continuation phases** (`address_review`) deliberately resume the
  implementer's session. Address-review applies code fixes to satisfy PR
  review feedback, continuing the same line of work the implementer started,
  so it passes ``AGENT_IMPLEMENTER`` to land on the same session UUID. This is
  intentional and is the mechanism that gives that phase a warm prompt cache.

These tests assert source-text properties (not runtime mock behavior)
because constructing valid Options objects for every phase is brittle and
orthogonal to what we want to guard: that the *wiring* is correct.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import hephaestus.automation as automation_pkg

AUTOMATION_DIR = Path(automation_pkg.__file__).parent


# Self-agent phases: module owns a unique session identity.
#
# Each entry is ``(module_file, expected_agent_constant, companion_files)``,
# where ``companion_files`` is an optional tuple of sibling modules that
# share the same self-agent identity. For ``implementer.py`` the
# per-issue phase runner was extracted into ``implementer_phase_runner.py``
# in #597 — both files participate in the implementer's session and must
# be inspected together. ``invoke_claude_with_session`` is called inside
# the runner; the ``AGENT_IMPLEMENTER`` constant is referenced through
# the implementer module's namespace (``_impl_mod.AGENT_IMPLEMENTER``)
# so test patches at ``implementer.AGENT_IMPLEMENTER`` still take effect.
SELF_AGENT_PHASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("implementer.py", "AGENT_IMPLEMENTER", ("implementer_phase_runner.py",)),
    # ci_driver owns Session 3 (AGENT_CI_DRIVER): its fix sessions and its
    # post-green learnings run on a transcript independent of the implementer.
    ("ci_driver.py", "AGENT_CI_DRIVER", ()),
]


# Per-iteration reviewer phases: a fresh session every review-loop iteration.
#
# Each entry is ``(module_file, base_agent_constant, in_loop_caller_files)``.
# The reviewer derives its per-iteration token via
# ``reviewer_agent(base, iteration)`` so successive iterations land on distinct
# session UUIDs and the reviewer never reviews its own prior verdict.
#
# The wrapping call site differs by reviewer: the plan reviewer's lives in the
# planner's in-loop driver (``planner_review_loop.py``), while the PR reviewer
# carries its own in-loop callable (``review_pr_inline``) inside
# ``pr_reviewer.py``. We scan the module together with its in-loop caller so the
# guard holds regardless of which file owns the wrapper.
PER_ITERATION_REVIEWER_PHASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("plan_reviewer.py", "AGENT_PLAN_REVIEWER", ("planner_review_loop.py",)),
    ("pr_reviewer.py", "AGENT_PR_REVIEWER", ()),
]


# Continuation phases: deliberately resume the implementer's session to get
# warm prompt cache while continuing the same line of work.
CONTINUATION_PHASES: list[str] = [
    "address_review.py",
]


def _read_phase_sources(module_file: str, companions: tuple[str, ...]) -> str:
    """Return the concatenated source of *module_file* and any companions."""
    parts = [(AUTOMATION_DIR / module_file).read_text()]
    parts.extend((AUTOMATION_DIR / c).read_text() for c in companions)
    return "\n".join(parts)


@pytest.mark.parametrize("module_file, expected_agent, companions", SELF_AGENT_PHASES)
def test_self_agent_phase_imports_expected_agent(
    module_file: str, expected_agent: str, companions: tuple[str, ...]
) -> None:
    """Each self-agent phase imports its dedicated AGENT_* constant.

    Imports may live in ``module_file`` itself or in any of its
    ``companions`` (e.g. ``implementer_phase_runner.py`` was extracted from
    ``implementer.py`` in #597 but still participates in the implementer's
    session identity).
    """
    src = _read_phase_sources(module_file, companions)
    import_pattern = re.compile(rf"from\s+\.session_naming\s+import\s+[^\n]*\b{expected_agent}\b")
    assert import_pattern.search(src), (
        f"{module_file} (and companions {companions}) must import {expected_agent} "
        f"from .session_naming"
    )


@pytest.mark.parametrize("module_file, expected_agent, companions", SELF_AGENT_PHASES)
def test_self_agent_phase_passes_expected_agent_kwarg(
    module_file: str, expected_agent: str, companions: tuple[str, ...]
) -> None:
    """Each self-agent phase passes its AGENT_* constant via ``agent=``.

    The implementer module dispatches the actual call through
    ``implementer_phase_runner`` and references the constant as
    ``_impl_mod.AGENT_IMPLEMENTER`` so test patches at
    ``hephaestus.automation.implementer.AGENT_IMPLEMENTER`` continue to
    work. The pattern below accepts both the bare ``AGENT_IMPLEMENTER``
    form and the namespaced ``X.AGENT_IMPLEMENTER`` form.
    """
    src = _read_phase_sources(module_file, companions)
    kwarg_pattern = re.compile(rf"\bagent\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?{expected_agent}\b")
    assert kwarg_pattern.search(src), (
        f"{module_file} (and companions {companions}) must pass agent={expected_agent} "
        "to invoke_claude_with_session"
    )


@pytest.mark.parametrize("module_file, expected_agent, companions", SELF_AGENT_PHASES)
def test_self_agent_phase_does_not_use_foreign_agent(
    module_file: str, expected_agent: str, companions: tuple[str, ...]
) -> None:
    """A self-agent phase must not resume any OTHER stage's session.

    It may pass its own ``expected_agent`` and ``AGENT_ADVISE`` — every stage
    opens its own cheap, read-only advise session as its first step (#30), which
    is shared infrastructure, not a foreign stage's transcript. Resuming any
    other stage's agent (e.g. the implementer landing on the planner's session)
    would be the bug this guards against.
    """
    allowed = {expected_agent, "AGENT_ADVISE"}
    src = _read_phase_sources(module_file, companions)
    found = set(re.findall(r"\bagent\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?(AGENT_[A-Z_]+)\b", src))
    assert found <= allowed, (
        f"{module_file} (and companions {companions}) uses unexpected AGENT_* "
        f"constants: {found - allowed}; expected only {allowed}"
    )


@pytest.mark.parametrize("module_file", CONTINUATION_PHASES)
def test_continuation_phase_resumes_implementer_session(module_file: str) -> None:
    """address_review deliberately resumes the implementer.

    Address-review applies code fixes that continue the implementer's line of
    work. Passing AGENT_IMPLEMENTER lands it on the implementer's deterministic
    session UUID, giving it a warm prompt cache. Any other AGENT_* constant
    here would create a fresh cold session and silently undo the cache reuse.
    """
    src = (AUTOMATION_DIR / module_file).read_text()
    found = set(re.findall(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b", src))
    assert found == {"AGENT_IMPLEMENTER"}, (
        f"{module_file} must pass agent=AGENT_IMPLEMENTER to continue the "
        f"implementer's session for warm-cache reuse; found {found}"
    )


def test_planner_module_uses_its_expected_agents() -> None:
    """planner.py drives multiple call sites with distinct agents.

    Stage 1 (#455/#468/#484) changed two of these:

    AGENT_PLANNER       — main planning call AND post-plan learnings capture.
                          Learnings now RESUME the planner's own session (it
                          previously opened a separate AGENT_LEARNINGS session)
                          so the model still "remembers" the plan it wrote.
    AGENT_ADVISE        — pre-plan advice call.
    AGENT_PLAN_REVIEWER — in-process plan-review call, now wrapped in
                          ``reviewer_agent(AGENT_PLAN_REVIEWER, iteration)`` so
                          the reviewer gets a FRESH session every iteration and
                          never re-reviews its own prior verdict. Because of the
                          wrapper there is no bare ``agent=AGENT_PLAN_REVIEWER``
                          assignment — see the dedicated assertion below.

    AGENT_LEARNINGS is intentionally NO LONGER used by the planner.
    """
    # The planner package was split (#598): the strict review loop lives in
    # planner_review_loop.py. Scan both source files for AGENT_* wiring so the
    # invariant survives the split.
    src = (AUTOMATION_DIR / "planner.py").read_text() + (
        AUTOMATION_DIR / "planner_review_loop.py"
    ).read_text()
    found = set(re.findall(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b", src))
    # Bare ``agent=AGENT_*`` assignments after Stage 1.
    expected = {
        "AGENT_PLANNER",
        "AGENT_ADVISE",
    }
    assert found == expected, f"planner agent wiring drifted: found={found}, expected={expected}"

    # The reviewer call must use the fresh-per-iteration wrapper, not a bare
    # ``agent=AGENT_PLAN_REVIEWER`` (which would resume the same session).
    assert re.search(r"\bagent\s*=\s*reviewer_agent\(\s*AGENT_PLAN_REVIEWER\s*,", src), (
        "in-loop reviewer must use agent=reviewer_agent(AGENT_PLAN_REVIEWER, iteration)"
    )

    # AGENT_LEARNINGS must no longer be wired into any planner call.
    assert "AGENT_LEARNINGS" not in found, (
        "learnings capture must resume AGENT_PLANNER, not open an AGENT_LEARNINGS session"
    )


@pytest.mark.parametrize("module_file, base_agent, in_loop_callers", PER_ITERATION_REVIEWER_PHASES)
def test_per_iteration_reviewer_uses_reviewer_agent_wrapper(
    module_file: str, base_agent: str, in_loop_callers: tuple[str, ...]
) -> None:
    """Each reviewer derives a FRESH session per iteration via reviewer_agent().

    A per-iteration reviewer must NOT pin a single session across the review
    loop — that is precisely the self-review bug (#455/#468/#484). Somewhere in
    its call path (the reviewer module itself or its in-loop caller) the token
    ``reviewer_agent(base, iteration)`` must be produced and forwarded into the
    Claude session call. We scan module + in-loop callers together because the
    wrapper lives in the planner loop for the plan reviewer but inside the
    module for the PR reviewer.
    """
    src = _read_phase_sources(module_file, in_loop_callers)
    assert re.search(r"from\s+\.session_naming\s+import\s+[^\n]*\breviewer_agent\b", src), (
        f"{module_file} (and callers {in_loop_callers}) must import reviewer_agent "
        f"from .session_naming"
    )
    assert re.search(rf"\b{base_agent}\b", src), (
        f"{module_file} (and callers {in_loop_callers}) must reference {base_agent}"
    )
    assert re.search(rf"\breviewer_agent\(\s*{base_agent}\s*,", src), (
        f"{module_file} (and callers {in_loop_callers}) must derive the per-iteration "
        f"session via reviewer_agent({base_agent}, iteration) so each review round "
        f"is a fresh session"
    )


@pytest.mark.parametrize("module_file, base_agent, in_loop_callers", PER_ITERATION_REVIEWER_PHASES)
def test_per_iteration_reviewer_does_not_pin_foreign_agent(
    module_file: str, base_agent: str, in_loop_callers: tuple[str, ...]
) -> None:
    """A reviewer module must not pin any non-reviewer AGENT_* via a bare agent=.

    Scoped to the reviewer module itself (NOT its callers — the planner loop
    legitimately uses AGENT_PLANNER/AGENT_ADVISE). The only bare
    ``agent=AGENT_*`` constant the reviewer module may pin is its own base
    (e.g. the standalone CLI default); it must never resume a foreign session
    such as ``AGENT_IMPLEMENTER``.
    """
    src = (AUTOMATION_DIR / module_file).read_text()
    found = set(re.findall(r"\bagent\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?(AGENT_[A-Z_]+)\b", src))
    assert found <= {base_agent}, (
        f"{module_file} pins unexpected AGENT_* constants via agent=: "
        f"{found - {base_agent}}; a reviewer module may only pin its own base {base_agent}"
    )


# Advise-first (#30): each stage opens its run with an /advise step gated by
# ``enable_advise``.  The planner and CI driver always run advise under
# AGENT_ADVISE (a cheap, separate read-only session).  The implementer has two
# paths: Codex still uses AGENT_ADVISE, but Claude uses AGENT_IMPLEMENTER as
# turn 1 so the findings live in the implementer's own transcript.
ADVISE_FIRST_STAGES_ADVISE_AGENT: list[tuple[str, tuple[str, ...]]] = [
    # Stage 1: the planner runs advise once before the plan loop.
    ("planner.py", ("planner_review_loop.py",)),
    # Stage 3: the CI driver runs advise before the fix loop.
    ("ci_driver.py", ()),
]


@pytest.mark.parametrize("module_file, companions", ADVISE_FIRST_STAGES_ADVISE_AGENT)
def test_stage_runs_advise_under_advise_agent(
    module_file: str, companions: tuple[str, ...]
) -> None:
    """Planner and CI driver run /advise under AGENT_ADVISE, gated by enable_advise.

    Advise-first (#30) is the mechanism that pulls prior learnings into each
    stage before it acts. For these stages the advise session always runs under
    ``AGENT_ADVISE`` (never the stage's own session), and every stage must let
    operators turn it off via ``enable_advise``.
    """
    src = _read_phase_sources(module_file, companions)
    assert "AGENT_ADVISE" in src, f"{module_file} must run its advise step under AGENT_ADVISE"
    assert "enable_advise" in src, f"{module_file} must gate advise behind enable_advise"


def test_implementer_runs_advise_as_first_turn_of_implementer_session() -> None:
    """The implementer runs /advise as turn 1 of the implementer's own session.

    Unlike the planner/CI-driver, the Claude implementer does NOT use a separate
    AGENT_ADVISE session.  Instead, _run_advise_as_implementer_turn sends the
    advise prompt to AGENT_IMPLEMENTER with cwd=worktree_path so the findings
    live in the same transcript that the implementation turn resumes from.

    Codex falls back to AGENT_ADVISE via _run_advise (no multi-turn session
    support), so AGENT_ADVISE must also remain present for that path.
    """
    src = _read_phase_sources("implementer_phase_runner.py", ())
    assert "AGENT_IMPLEMENTER" in src, (
        "implementer_phase_runner.py must route Claude advise under AGENT_IMPLEMENTER"
    )
    assert "_run_advise_as_implementer_turn" in src, (
        "implementer_phase_runner.py must implement _run_advise_as_implementer_turn"
    )
    assert "AGENT_ADVISE" in src, (
        "implementer_phase_runner.py must keep AGENT_ADVISE for the Codex fallback path"
    )
    assert "enable_advise" in src, (
        "implementer_phase_runner.py must gate advise behind enable_advise"
    )
