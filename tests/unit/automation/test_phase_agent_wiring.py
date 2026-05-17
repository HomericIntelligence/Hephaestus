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
SELF_AGENT_PHASES: list[tuple[str, str]] = [
    ("plan_reviewer.py", "AGENT_PLAN_REVIEWER"),
    ("pr_reviewer.py", "AGENT_PR_REVIEWER"),
    ("implementer.py", "AGENT_IMPLEMENTER"),
]


# Continuation phases: deliberately resume the implementer's session to get
# warm prompt cache while continuing the same line of work.
CONTINUATION_PHASES: list[str] = [
    "address_review.py",
    "ci_driver.py",
]


@pytest.mark.parametrize("module_file, expected_agent", SELF_AGENT_PHASES)
def test_self_agent_phase_imports_expected_agent(module_file: str, expected_agent: str) -> None:
    """Each self-agent phase imports its dedicated AGENT_* constant."""
    src = (AUTOMATION_DIR / module_file).read_text()
    import_pattern = re.compile(rf"from\s+\.session_naming\s+import\s+[^\n]*\b{expected_agent}\b")
    assert import_pattern.search(src), (
        f"{module_file} must import {expected_agent} from .session_naming"
    )


@pytest.mark.parametrize("module_file, expected_agent", SELF_AGENT_PHASES)
def test_self_agent_phase_passes_expected_agent_kwarg(
    module_file: str, expected_agent: str
) -> None:
    """Each self-agent phase passes its AGENT_* constant via ``agent=``."""
    src = (AUTOMATION_DIR / module_file).read_text()
    kwarg_pattern = re.compile(rf"\bagent\s*=\s*{expected_agent}\b")
    assert kwarg_pattern.search(src), (
        f"{module_file} must pass agent={expected_agent} to invoke_claude_with_session"
    )


@pytest.mark.parametrize("module_file, expected_agent", SELF_AGENT_PHASES)
def test_self_agent_phase_does_not_use_foreign_agent(module_file: str, expected_agent: str) -> None:
    """A self-agent phase must not pass any other AGENT_* constant."""
    src = (AUTOMATION_DIR / module_file).read_text()
    found = set(re.findall(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b", src))
    assert found <= {expected_agent}, (
        f"{module_file} uses unexpected AGENT_* constants: "
        f"{found - {expected_agent}}; expected only {expected_agent}"
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
    src = (AUTOMATION_DIR / "planner.py").read_text()
    found = set(re.findall(r"\bagent\s*=\s*(AGENT_[A-Z_]+)\b", src))
    expected = {
        "AGENT_PLANNER",
        "AGENT_ADVISE",
        "AGENT_LEARNINGS",
        "AGENT_PLAN_REVIEWER",
    }
    assert found == expected, f"planner.py agent wiring drifted: found={found}, expected={expected}"
