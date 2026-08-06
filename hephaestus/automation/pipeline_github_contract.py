"""Static host contract shared by the GitHub adapter collaborators."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path
    from typing import Any, Protocol

    from hephaestus.automation.arming_state import ArmingStateStore

    class _PipelineGitHubHost(Protocol):
        """State and cross-collaborator methods supplied by ``PipelineGitHub``."""

        org: str
        repo: str | None
        dry_run: bool
        _repo_root: Path
        _arming: ArmingStateStore

        @property
        def _repo_slug(self) -> str | None:
            pass

        def _owner_name(self) -> tuple[str, str]:
            pass

        def _viewer_login(self) -> str:
            pass

        def _comment_owned_by_viewer(self, comment: dict[str, Any]) -> bool:
            pass

        def _graphql(self, query: str, **fields: int | str) -> dict[str, Any]:
            pass

        def _gh(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            pass

        def _label_names(self) -> set[str]:
            pass

        def _create_label(self, name: str) -> None:
            pass

        def _add_labels(self, issue_number: int, labels: list[str]) -> None:
            pass

        def _remove_labels(self, issue_number: int, labels: list[str]) -> None:
            pass

        @staticmethod
        def _label_names_from_payload(payload: dict[str, Any]) -> list[str]:
            pass

        def _skip(self, what: str) -> bool:
            pass

        def _open_prs_for_branch(self, branch_name: str) -> list[tuple[int, str]]:
            pass

        def _repo_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
            pass

        def find_pr_for_issue(self, issue_number: int) -> int | None:
            pass

        def pr_has_implementation_state_label(self, pr_number: int) -> tuple[bool, bool]:
            pass

        def gh_pr_state(self, pr_number: int) -> dict[str, Any] | None:
            pass

        @staticmethod
        def _thread_comment_snapshot(
            thread: dict[str, Any],
        ) -> tuple[tuple[str, str], ...] | None:
            pass

        def _review_thread_snapshot(self, pr_number: int, thread_id: str) -> dict[str, Any] | None:
            pass

        def upsert_issue_comment(
            self,
            issue_number: int,
            marker: str,
            body: str,
            *,
            legacy_marker: str | None = None,
        ) -> None:
            pass

else:

    class _PipelineGitHubHost:
        """Runtime-empty base for the statically checked host contract."""
