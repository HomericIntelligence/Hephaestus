"""Regression guard: each phase module passes the correct AGENT_* constant.

The phase modules split into three categories:

- **Self-agent phases** (`implementer`, `ci_driver`) own one long-lived
  session identity — each passes its dedicated ``AGENT_*`` constant to
  ``invoke_claude_with_session`` so its session UUID is distinct from every
  other phase's AND stable across the stage (resumed, not recreated).
  ``ci_driver`` owns Session 3 (``AGENT_CI_DRIVER``): drive-green polls CI,
  runs its own fix sessions, and captures its own learnings on a transcript
  independent of the implementer.
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
# ``invoke_claude_with_session`` callsites live in the phase modules
# (``_implement_phase.py`` / ``_review_phase.py``); ``implementer.py`` itself is
# now a thin pipeline CLI wrapper (#1821) that carries no agent identity, so the
# ``AGENT_IMPLEMENTER`` import and ``agent=`` kwarg are inspected on the
# companions. Each phase module imports and references ``AGENT_IMPLEMENTER``
# directly (no longer through the implementer module's namespace).
SELF_AGENT_PHASES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "implementer.py",
        "AGENT_IMPLEMENTER",
        (
            "_implement_phase.py",
            "_review_phase.py",
        ),
    ),
    # ci_driver owns Session 3 (AGENT_CI_DRIVER): the live AGENT_CI_DRIVER
    # imports moved into the extracted collaborators ci_fix_orchestrator (fix
    # sessions) and post_merge_processor (post-green /learn + /compact) in the
    # CIDriver decomposition (#1357 / refs #1179, #1289). ci_driver.py remains
    # the orchestrating entry point; both run on a transcript independent of the
    # implementer.
    (
        "ci_driver.py",
        "AGENT_CI_DRIVER",
        ("ci_fix_orchestrator.py", "post_merge_processor.py"),
    ),
]


# Continuation phases: deliberately resume the implementer's session to get
# warm prompt cache while continuing the same line of work.
CONTINUATION_PHASES: list[str] = [
    # The address-review fix session (agent=AGENT_IMPLEMENTER, warm-cache resume)
    # moved into address_review_core.py in the #1823 omit-reduction split;
    # address_review.py is now a thin re-export wrapper over the core.
    "address_review_core.py",
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
    ``companions`` (e.g. ``_implement_phase.py`` / ``_review_phase.py`` hold the
    implementer session's ``invoke_claude_with_session`` callsites, so they carry
    the ``AGENT_IMPLEMENTER`` identity for the thin ``implementer.py`` wrapper).
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

    The implementer session's actual dispatch lives in the phase modules
    (``_implement_phase.py`` / ``_review_phase.py``), which import the constant
    directly and reference it as the bare ``AGENT_IMPLEMENTER``. The pattern
    below accepts both the bare ``AGENT_IMPLEMENTER`` form and the namespaced
    ``X.AGENT_IMPLEMENTER`` form for resilience.
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


# The planner's per-agent session wiring (AGENT_PLANNER / AGENT_ADVISE /
# AGENT_PLAN_REVIEWER) moved into the queue-based pipeline stages when
# ``hephaestus-plan-issues`` was re-pointed at the pipeline (#1820): the plan
# and learn calls live in ``pipeline/stages/planning.py`` and
# ``pipeline/stages/plan_review.py`` (both asserted by their own stage tests).
# ``planner.py`` is now a thin CLI wrapper with no ``agent=`` call sites, so the
# former ``test_planner_module_uses_its_expected_agents`` guard was removed.


def test_implementer_prepends_advise_context_for_all_agents() -> None:
    """The implementation stage injects selected-skill context into the prompt.

    The advise wiring moved from the deleted legacy phase runner into
    the pipeline implementation stage (#1821): it gates the advise step behind
    ``ctx.config.enable_advise`` and composes the findings block via
    ``build_implementation_prompt``.
    """
    stage_src = (AUTOMATION_DIR / "pipeline" / "stages" / "implementation.py").read_text()
    assert "enable_advise" in stage_src, (
        "implementation stage must gate advise behind enable_advise"
    )
    assert "build_implementation_prompt" in stage_src, (
        "implementation stage must inject advise findings into the prompt"
    )
