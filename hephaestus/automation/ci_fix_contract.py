"""Static host contract shared by the CI-fix collaborators."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any, Protocol

    class _CIFixHost(Protocol):
        """Provider state and methods supplied by ``CIFixOrchestrator``."""

        _options: Callable[[], Any]
        _state_dir: Callable[[], Path]
        _failing_required_check_names: Callable[[int], list[str]]
        _format_review_threads_block: Callable[[int], str]

        def retry_no_commit_once(
            self,
            *,
            issue_number: int,
            pr_number: int,
            worktree_path: Path,
            pr_head_branch: str,
            pre_agent_sha: str,
            session_id: str | None,
            max_retries: int = 2,
        ) -> bool:
            pass

        def push_ci_fix(
            self,
            *,
            worktree_path: Path,
            pre_agent_sha: str,
            issue_number: int,
            pr_number: int,
            pr_head_branch: str,
            session_id: str | None,
            pr_base_branch: str = "main",
            ci_logs: str = "",
        ) -> bool:
            pass

else:

    class _CIFixHost:
        """Runtime-empty base for the statically checked host contract."""
