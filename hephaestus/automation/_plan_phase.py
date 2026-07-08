"""Plan presence + generation phase for the implementation pipeline.

Part of the #712 phase decomposition (the per-issue control flow it once
fed now lives in the pipeline stages, epic #1809).
:class:`PlanPhase` owns the "does this issue already have an
implementation plan, and if not, generate one" responsibility.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hephaestus.github.client import gh_call

from ._stage_context import StageMixin
from .agent_config import plan_stage_timeout
from .git_utils import run
from .planner_state import _comments_contain_plan

if TYPE_CHECKING:
    from ._stage_context import StageContext


def _phase_env(repo_root: Path) -> dict[str, str]:
    """Return a sanitized environment for a phase subprocess.

    Child phase invocations must not inherit a ``PYTHONPATH`` that can place
    third-party ``site-packages`` ahead of the stdlib. Keep only the repo root
    so source-checkout fallback still works without the ambient search path.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    return env


class PlanPhase(StageMixin):
    """Ensure an issue has an implementation plan before implementation."""

    def __init__(self, ctx: StageContext) -> None:
        """Store the shared :class:`StageContext`."""
        self.ctx = ctx

    def _has_plan(self, issue_number: int) -> bool:
        """Check if issue has an implementation plan.

        Delegates to :func:`planner_state._comments_contain_plan` so the
        prefix-anchored check stays in sync with the planner. Substring
        matching here previously caused the implementer to mistake a
        ``## 🔍 Plan Review`` comment (which quotes the plan body) for the
        plan itself — the same bug class fixed in #455/#468/#484 (#715).

        Note: ``_comments_contain_plan`` is a private helper but is the
        canonical implementation per its own docstring; cross-module reuse
        here is intentional to avoid a third copy of the same prefix logic.
        """
        try:
            result = gh_call(
                ["issue", "view", str(issue_number), "--comments", "--json", "comments"]
            )
            data = json.loads(result.stdout)
            comments = data.get("comments", [])
            return _comments_contain_plan(comments)
        except (subprocess.SubprocessError, RuntimeError, json.JSONDecodeError, OSError):
            return False

    def _generate(self, issue_number: int) -> None:
        """Generate plan for an issue using hephaestus-plan-issues.

        The plan-issues subprocess is bounded by a stage-level wrapper timeout
        (default 7200s, ``HEPH_PLAN_STAGE_TIMEOUT``-tunable) instead of the
        inner planner-agent timeout. A heavy god-class issue can exceed 1200s of
        total planner runtime while individual planner agent calls still use
        their shorter ``AGENT_PLAN_TIMEOUT`` budget (#1374).
        """
        import shutil

        plan_timeout = plan_stage_timeout()

        # Prefer the installed entry point (works in any repo)
        entry_point = shutil.which("hephaestus-plan-issues")
        if entry_point:
            run(
                [entry_point, "--issues", str(issue_number), "--agent", self.options.agent],
                timeout=plan_timeout,
                env=_phase_env(self.repo_root),
            )
            return

        # Fall back to python -m invocation using the sanitized repo-root-only
        # PYTHONPATH (source-checkout fallback). On failure, fall through to the
        # legacy scripts/plan_issues.py path.
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
                timeout=plan_timeout,
                env=_phase_env(self.repo_root),
            )
            return

        # Legacy fallback: local scripts/plan_issues.py (ProjectScylla layout)
        plan_script = self.repo_root / "scripts" / "plan_issues.py"
        if plan_script.exists():
            run(
                [sys.executable, str(plan_script), "--issues", str(issue_number)],
                timeout=plan_timeout,
                env=_phase_env(self.repo_root),
            )
            return

        raise RuntimeError(
            "Could not find hephaestus-plan-issues entry point, "
            "hephaestus.automation.planner module, or "
            f"scripts/plan_issues.py in {self.repo_root}"
        )
