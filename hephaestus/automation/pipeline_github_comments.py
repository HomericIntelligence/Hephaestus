# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405
import subprocess
from collections.abc import Callable

from hephaestus.automation.comment_identity import (
    CommentAliasConflictError,
    has_marker_alias,
    is_current_planning_marker,
    is_planning_marker,
    select_unambiguous_comment,
    validate_planning_body_for_write,
    validate_planning_comment_identities,
)
from hephaestus.automation.protocol import comment_marker_aliases
from hephaestus.automation.requirements_recovery import (
    RECOVERY_PROVENANCE_PREFIX,
    parse_recovery_provenance,
)

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *
from .review_journal import has_exact_leading_marker


def _validate_shared_planning_identities(
    comments: list[dict[str, Any]],
    *,
    planning_marker: bool,
    owned_of: Callable[[dict[str, Any]], bool],
) -> None:
    """Reject unsafe shared planning identities before an upsert mutation."""
    if planning_marker:
        validate_planning_comment_identities(
            comments,
            body_of=lambda comment: str(comment.get("body", "")),
            owned_of=owned_of,
        )


def _has_valid_leading_marker(body: str, marker: str) -> bool:
    """Validate fixed markers and the sealed recovery-provenance prefix."""
    if marker == RECOVERY_PROVENANCE_PREFIX:
        return body.startswith(marker) and parse_recovery_provenance(body) is not None
    return has_exact_leading_marker(body, marker)


def _select_owned_comment(
    comments: list[dict[str, Any]],
    *,
    marker: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    """Select one owned comment for a fixed marker or sealed recovery prefix."""
    if marker == RECOVERY_PROVENANCE_PREFIX:
        if len(comments) > 1:
            raise CommentAliasConflictError(
                f"ambiguous actor-owned comment aliases for {marker!r}; manual recovery is required"
            )
        return comments[0] if comments else None
    return select_unambiguous_comment(
        comments,
        marker=marker,
        aliases=aliases,
        body_of=lambda comment: str(comment.get("body", "")),
    )


def _reject_malformed_owned_recovery_provenance(
    comments: list[dict[str, Any]],
    *,
    owned_of: Callable[[dict[str, Any]], bool],
) -> None:
    """Fail closed when an actor-owned recovery prefix has an invalid seal."""
    for comment in comments:
        body = str(comment.get("body", ""))
        if (
            body.startswith(RECOVERY_PROVENANCE_PREFIX)
            and owned_of(comment)
            and parse_recovery_provenance(body) is None
        ):
            raise RuntimeError(
                "malformed actor-owned recovery provenance; manual recovery is required"
            )


def _validate_recovery_provenance_comments(
    marker: str,
    comments: list[dict[str, Any]],
    *,
    owned_of: Callable[[dict[str, Any]], bool],
) -> None:
    """Validate recovery-specific comment authority when that role is selected."""
    if marker == RECOVERY_PROVENANCE_PREFIX:
        _reject_malformed_owned_recovery_provenance(comments, owned_of=owned_of)


class PipelineGitHubIssueComments(_PipelineGitHubHost):
    """Own issue-comment mutations with planning identity checks."""

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the actor-owned current plan by its opaque marker."""
        self.upsert_issue_comment(
            issue_number,
            PLAN_CANONICAL_MARKER,
            body,
        )

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Upsert one actor-owned canonical comment keyed on an opaque marker.

        A shared or legacy planning marker on a foreign or unverifiable
        comment is an identity conflict and stops the upsert. Other
        human-authored marker collisions remain inert: they are not trusted,
        patched, or deleted. Historical heading-only comments are never
        migration candidates.
        """
        try:
            self._upsert_issue_comment(
                issue_number,
                marker,
                body,
                legacy_marker=legacy_marker,
            )
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"failed to upsert issue #{issue_number} comment {marker!r}: {exc}"
            ) from exc

    def _upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Execute canonical comment upsert after the public error boundary."""
        planning_marker = is_planning_marker(marker)
        if planning_marker and not is_current_planning_marker(marker):
            raise ValueError("new planning comments must use a shared HomericIntelligence marker")
        marker_aliases = comment_marker_aliases(marker)
        if planning_marker and legacy_marker is not None and legacy_marker not in marker_aliases:
            raise ValueError(
                f"legacy marker {legacy_marker!r} does not belong to planning role {marker!r}"
            )
        markers = tuple(
            dict.fromkeys((*marker_aliases, *((legacy_marker,) if legacy_marker else ())))
        )

        def owned_matching(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                comment
                for comment in comments
                if (
                    _has_valid_leading_marker(str(comment.get("body", "")), marker)
                    if marker == RECOVERY_PROVENANCE_PREFIX
                    else has_marker_alias(str(comment.get("body", "")), markers)
                )
                and self._comment_owned_by_viewer(comment)
            ]

        if not _has_valid_leading_marker(body, marker):
            raise ValueError(f"canonical comment body must start with marker {marker!r}")
        validate_planning_body_for_write(marker, body)
        if self._skip(f"upsert {marker!r} comment on #{issue_number}"):
            return
        comments = self._repo_issue_comments(issue_number)
        _validate_recovery_provenance_comments(
            marker, comments, owned_of=self._comment_owned_by_viewer
        )
        _validate_shared_planning_identities(
            comments,
            planning_marker=planning_marker,
            owned_of=self._comment_owned_by_viewer,
        )
        owned = owned_matching(comments)
        target = _select_owned_comment(owned, marker=marker, aliases=markers)
        if target is None:
            self._post_issue_comment(issue_number, body)
            comments = self._repo_issue_comments(issue_number)
            _validate_recovery_provenance_comments(
                marker, comments, owned_of=self._comment_owned_by_viewer
            )
            _validate_shared_planning_identities(
                comments,
                planning_marker=planning_marker,
                owned_of=self._comment_owned_by_viewer,
            )
            owned = owned_matching(comments)
            target = _select_owned_comment(owned, marker=marker, aliases=markers)
            if target is None:
                raise RuntimeError(f"owned comment publication was not confirmed for {marker!r}")

        target_id = target.get("databaseId")
        if target_id is None:
            raise RuntimeError(f"owned comment for {marker!r} has no database id")
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        if str(target.get("body", "")) != body:
            self._patch_issue_comment(int(target_id), body, repo=(owner, name))
            comments = self._repo_issue_comments(issue_number)
            _validate_recovery_provenance_comments(
                marker, comments, owned_of=self._comment_owned_by_viewer
            )
            _validate_shared_planning_identities(
                comments,
                planning_marker=planning_marker,
                owned_of=self._comment_owned_by_viewer,
            )
            owned = owned_matching(comments)
            target = _select_owned_comment(owned, marker=marker, aliases=markers)
        if (
            target is None
            or target.get("databaseId") != target_id
            or str(target.get("body", "")) != body
        ):
            raise RuntimeError(f"owned comment publication was not confirmed for {marker!r}")

    def _post_issue_comment(self, issue_number: int, body: str) -> None:
        """Post one issue comment in the adapter's configured repository."""
        if self._repo_slug is not None:
            with github_api._body_file(body) as path:
                self._gh(["issue", "comment", str(issue_number), "--body-file", path])
            return
        github_api.gh_issue_comment(issue_number, body)

    def _patch_issue_comment(
        self,
        comment_id: int,
        body: str,
        *,
        repo: tuple[str, str] | None = None,
    ) -> None:
        """Replace one known actor-owned comment body."""
        owner, name = repo or (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        with github_api._body_file(body) as path:
            gh_call(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"/repos/{owner}/{name}/issues/comments/{comment_id}",
                    "-F",
                    f"body=@{path}",
                ],
                timeout=self._gh_timeout,
            )

    def _delete_issue_comment(self, comment_id: int) -> None:
        """Delete one duplicate actor-owned comment in the configured repository."""
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        github_api.gh_issue_delete_comment(
            comment_id,
            repo=(owner, name),
            missing_ok=True,
        )

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Append an immutable actor-owned artifact once, failing on mismatched replay."""
        if not has_exact_leading_marker(body, marker):
            raise ValueError(f"immutable comment body must start with marker {marker!r}")
        validate_planning_body_for_write(marker, body)
        if self._skip(f"append immutable {marker!r} comment on #{issue_number}"):
            return
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if has_exact_leading_marker(str(comment.get("body", "")), marker)
            and self._comment_owned_by_viewer(comment)
        ]
        if matching:
            if any(str(comment.get("body", "")) != body for comment in matching):
                raise RuntimeError(f"immutable journal conflict for marker {marker!r}")
            # This primitive still supports immutable non-issue artifacts.
            # Identical actor-owned copies can arise from a create race.
            return
        self._post_issue_comment(issue_number, body)
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if has_exact_leading_marker(str(comment.get("body", "")), marker)
            and self._comment_owned_by_viewer(comment)
        ]
        if any(str(comment.get("body", "")) != body for comment in matching):
            raise RuntimeError(f"immutable journal conflict for marker {marker!r}")
