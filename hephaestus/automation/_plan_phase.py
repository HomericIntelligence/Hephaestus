"""Plan presence + generation phase for the implementation pipeline.

Part of the #712 phase decomposition (the per-issue control flow it once
fed now lives in the pipeline stages, epic #1809).
:class:`PlanPhase` owns the "does this issue already have an
implementation plan, and if not, generate one" responsibility.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hephaestus.config.child_environments import build_python_phase_env

from ._stage_context import StageMixin
from .agent_config import plan_stage_timeout
from .git_utils import run
from .github_api import fetch_issue_comments_metadata, gh_current_login
from .review_journal import (
    PlanDiscoveryResult,
    discover_plan_from_comments,
    normalize_issue_comments,
)

if TYPE_CHECKING:
    from ._stage_context import StageContext


def _phase_env(repo_root: Path) -> dict[str, str]:
    """Return a sanitized environment for a phase subprocess.

    Child phase invocations must not inherit a ``PYTHONPATH`` that can place
    third-party ``site-packages`` ahead of the stdlib. Keep only the repo root
    so source-checkout fallback still works without the ambient search path.
    """
    return build_python_phase_env(repo_root)


class PlanPhase(StageMixin):
    """Ensure an issue has an implementation plan before implementation."""

    def __init__(self, ctx: StageContext) -> None:
        """Store the shared :class:`StageContext`."""
        self.ctx = ctx

    def _discover_plan(self, issue_number: int) -> PlanDiscoveryResult:
        """Discover an actor-owned plan without converting read failure to absence."""
        try:
            comments = normalize_issue_comments(
                fetch_issue_comments_metadata(issue_number),
                viewer_login=gh_current_login() or "",
            )
        except Exception as exc:
            return PlanDiscoveryResult.read_error(exc)
        return discover_plan_from_comments(comments)

    def _generate(self, issue_number: int) -> None:
        """Generate plan for an issue using hephaestus-plan-issues.

        The plan-issues subprocess is bounded by a stage-level wrapper timeout
        (default 7200s) instead of the
        inner planner-agent timeout. A heavy god-class issue can exceed 1200s of
        total planner runtime while individual planner agent calls still use
        their shorter ``AGENT_PLAN_TIMEOUT`` budget (#1374).
        """
        plan_timeout = getattr(self.options, "plan_stage_timeout", None)
        if plan_timeout is None:
            plan_timeout = plan_stage_timeout()

        # Invoke the planner through the active interpreter. Resolving a console
        # script from PATH can escape the uv project environment and select an
        # unrelated globally installed Hephaestus package.
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            run(
                [
                    sys.executable,
                    "-m",
                    "hephaestus.automation.planner",
                    "--issues",
                    str(issue_number),
                    "--agent",
                    self.options.agent,
                ],
                cwd=self.repo_root,
                timeout=plan_timeout,
                env=_phase_env(self.repo_root),
            )
            return

        # Legacy fallback: local scripts/plan_issues.py (Scylla layout)
        plan_script = self.repo_root / "scripts" / "plan_issues.py"
        if plan_script.exists():
            run(
                [sys.executable, str(plan_script), "--issues", str(issue_number)],
                cwd=self.repo_root,
                timeout=plan_timeout,
                env=_phase_env(self.repo_root),
            )
            return

        raise RuntimeError(
            "Could not find hephaestus-plan-issues entry point, "
            "hephaestus.automation.planner module, or "
            f"scripts/plan_issues.py in {self.repo_root}"
        )
