"""Regression guard: each phase module passes the correct AGENT_* constant.

The phase modules split into two categories:

- **Self-agent phases** (`plan_reviewer`, `pr_reviewer`, `implementer`) own
  their own session identity — each passes its dedicated ``AGENT_*`` constant
  to ``invoke_claude_with_session`` so its session UUID is distinct from
  every other phase's.
- **Continuation phases** (`address_review`, `ci_driver`) deliberately resume
  the implementer's session. Address-review applies code fixes to satisfy PR
  review feedback; ci_driver applies code fixes to make CI green. Both
  continue the same line of work the implementer started, so both pass
  ``AGENT_IMPLEMENTER`` to land on the same session UUID. This is intentional
  and is the mechanism that gives those phases warm prompt cache.

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
    ("plan_reviewer.py", "AGENT_PLAN_REVIEWER", ()),
    ("pr_reviewer.py", "AGENT_PR_REVIEWER", ()),
    ("implementer.py", "AGENT_IMPLEMENTER", ("implementer_phase_runner.py",)),
]


# Continuation phases: deliberately resume the implementer's session to get
# warm prompt cache while continuing the same line of work.
CONTINUATION_PHASES: list[str] = [
    "address_review.py",
    "ci_driver.py",
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
    """A self-agent phase must not pass any other AGENT_* constant."""
    src = _read_phase_sources(module_file, companions)
    found = set(re.findall(r"\bagent\s*=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?(AGENT_[A-Z_]+)\b", src))
    assert found <= {expected_agent}, (
        f"{module_file} (and companions {companions}) uses unexpected AGENT_* "
        f"constants: {found - {expected_agent}}; expected only {expected_agent}"
    )


@pytest.mark.parametrize("module_file", CONTINUATION_PHASES)
def test_continuation_phase_resumes_implementer_session(module_file: str) -> None:
    """address_review and ci_driver deliberately resume the implementer.

    Both phases apply code fixes that continue the implementer's line of work.
    Passing AGENT_IMPLEMENTER lands them on the implementer's deterministic
    session UUID, giving them a warm prompt cache. Any other AGENT_* constant
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

    AGENT_PLANNER       — main planning call
    AGENT_ADVISE        — pre-plan advice call
    AGENT_LEARNINGS     — post-plan learnings capture
    AGENT_PLAN_REVIEWER — in-process plan-review call (separate from the
                          standalone plan_reviewer.py phase module)
    """
    # The planner package was split (#598): the strict review loop lives in
    # planner_review_loop.py. Scan both source files for AGENT_* wiring so the
    # invariant survives the split.
    src = (AUTOMATION_DIR / "planner.py").read_text() + (
        AUTOMATION_DIR / "planner_review_loop.py"
    ).read_text()
    found = set(re.findall(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b", src))
    expected = {
        "AGENT_PLANNER",
        "AGENT_ADVISE",
        "AGENT_LEARNINGS",
        "AGENT_PLAN_REVIEWER",
    }
    assert found == expected, f"planner agent wiring drifted: found={found}, expected={expected}"
