"""Plan presence + generation phase for the implementation pipeline.

Extracted from :class:`ImplementationPhaseRunner` as part of the #712
decomposition. :class:`PlanPhase` owns the "does this issue already have an
implementation plan, and if not, generate one" responsibility.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from typing import TYPE_CHECKING

from hephaestus.github.client import gh_call

from ._stage_context import StageMixin
from .claude_timeouts import planner_claude_timeout
from .git_utils import run
from .planner_state import _comments_contain_plan

if TYPE_CHECKING:
    from ._stage_context import StageContext


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

        The plan-issues subprocess is bounded by the centralized
        :func:`hephaestus.automation.claude_timeouts.planner_claude_timeout`
        (default 7200s, ``HEPH_PLANNER_AGENT_TIMEOUT``-tunable) rather than a
        hard-coded 600s. A heavy god-class issue can exceed 600s of agent time
        while still inside the planner's own 7200s budget; the old 600s wrapper
        killed the subprocess prematurely and the loop retried the whole phase
        with no backoff (#1374).
        """
        import shutil

        plan_timeout = planner_claude_timeout()

        # Prefer the installed entry point (works in any repo)
        entry_point = shutil.which("hephaestus-plan-issues")
        if entry_point:
            run(
                [entry_point, "--issues", str(issue_number), "--agent", self.options.agent],
                timeout=plan_timeout,
            )
            return

        # Fall back to python -m invocation (works when PYTHONPATH is set).
        # On failure, fall through to the legacy scripts/plan_issues.py path.
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
            )
            return

        # Legacy fallback: local scripts/plan_issues.py (ProjectScylla layout)
        plan_script = self.repo_root / "scripts" / "plan_issues.py"
        if plan_script.exists():
            run(
                [sys.executable, str(plan_script), "--issues", str(issue_number)],
                timeout=plan_timeout,
            )
            return

        raise RuntimeError(
            "Could not find hephaestus-plan-issues entry point, "
            "hephaestus.automation.planner module, or "
            f"scripts/plan_issues.py in {self.repo_root}"
        )
