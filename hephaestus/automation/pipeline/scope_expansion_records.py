"""Immutable rendering and parsing for scope-expansion GitHub records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from hephaestus.automation.scope_expansion_domain import (
    ScopeExpansion,
    scope_expansion_digest,
    scope_expansion_marker,
)

SCOPE_EXPANSION_CHILD_MARKER_PREFIX = "<!-- hephaestus-scope-expansion-child:"
SCOPE_EXPANSION_LIFECYCLE_MARKER_PREFIX = "<!-- hephaestus-scope-expansion-lifecycle:"
SCOPE_EXPANSION_BLOCKING_REVIEW_MARKER_PREFIX = "<!-- hephaestus-scope-expansion-blocking-review:"
SCOPE_EXPANSION_RETRACTION_MARKER_PREFIX = "<!-- hephaestus-scope-expansion-retraction-projection:"
ScopeExpansionLifecycleState = Literal["pending-child", "pending-review", "blocked"]
ScopeExpansionReviewAction = Literal[
    "none", "parked", "operator_required", "sync_required", "fresh_review"
]

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_FULL_MARKER_RE = re.compile(r"<!-- hephaestus-scope-expansion-[a-z-]+:([0-9a-f]{64}) -->")
_VERSION = 1


@dataclass(frozen=True, slots=True)
class ScopeExpansionLifecycleRecord:
    """Durable source-PR record for one deterministic scope-expansion child."""

    version: int
    state: ScopeExpansionLifecycleState
    repository: str
    parent_issue: int
    pr_number: int
    reviewed_head_sha: str
    digest: str
    child_issue_number: int | None = None
    merge_sha: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeExpansionChildIssueRecord:
    """Rendered child-issue body and the normalized expansion content."""

    version: int
    repository: str
    parent_issue: int
    pr_number: int
    reviewed_head_sha: str
    digest: str
    expansion: ScopeExpansion
    child_issue_number: int | None = None


@dataclass(frozen=True, slots=True)
class ScopeExpansionBlockingReviewRecord:
    """Rendered blocking review body that links one durable child issue."""

    version: int
    repository: str
    parent_issue: int
    pr_number: int
    reviewed_head_sha: str
    child_issue_number: int
    digest: str


def _positive_identifier(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _full_sha(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _FULL_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase commit SHA")


def _parse_marker(body: object, prefix: str) -> tuple[str, list[str]] | None:
    if not isinstance(body, str):
        return None
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or not lines[0].startswith(prefix) or not lines[0].endswith("-->"):
        return None
    marker_match = _FULL_MARKER_RE.fullmatch(lines[0])
    if marker_match is None:
        return None
    return marker_match.group(1), lines[1:]


def scope_expansion_child_marker(
    repository: str, parent_issue: int, expansion: ScopeExpansion
) -> str:
    """Return the hidden marker line for one child issue body."""
    return scope_expansion_marker(repository, parent_issue, expansion).replace(
        "scope-expansion:", "scope-expansion-child:"
    )


def scope_expansion_lifecycle_marker(
    repository: str, parent_issue: int, expansion: ScopeExpansion
) -> str:
    """Return the hidden marker line for one source-PR lifecycle comment."""
    return scope_expansion_marker(repository, parent_issue, expansion).replace(
        "scope-expansion:", "scope-expansion-lifecycle:"
    )


def scope_expansion_blocking_review_marker(
    repository: str, parent_issue: int, expansion: ScopeExpansion
) -> str:
    """Return the hidden marker line for one blocking review body."""
    return scope_expansion_marker(repository, parent_issue, expansion).replace(
        "scope-expansion:", "scope-expansion-blocking-review:"
    )


def render_scope_expansion_child_body(
    *,
    repository: str,
    parent_issue: int,
    pr_number: int,
    reviewed_head_sha: str,
    expansion: ScopeExpansion,
    child_issue_number: int | None = None,
) -> str:
    """Render the immutable body for one child issue."""
    _positive_identifier(parent_issue, "parent_issue")
    _positive_identifier(pr_number, "pr_number")
    _full_sha(reviewed_head_sha, "reviewed_head_sha")
    digest = scope_expansion_digest(repository, parent_issue, expansion)
    lines = [
        f"<!-- hephaestus-scope-expansion-child:{digest} -->",
        "## Scope expansion child",
        f"Version: {_VERSION}",
        f"Repository: {repository.strip().lower()}",
        f"Parent issue: #{parent_issue}",
        f"Source PR: #{pr_number}",
        f"Reviewed head: `{reviewed_head_sha}`",
    ]
    if child_issue_number is not None:
        _positive_identifier(child_issue_number, "child_issue_number")
        lines.append(f"Child issue: #{child_issue_number}")
    lines.extend(
        [
            f"Title: {expansion.title}",
            f"Reason: {expansion.reason}",
            f"Source path: `{expansion.source_path}`",
            f"Source line: {expansion.source_line}",
            "Required paths:",
            *[f"- `{path}`" for path in expansion.required_paths],
            "Acceptance criteria:",
            *[f"- {criterion}" for criterion in expansion.acceptance_criteria],
            f"Blocks PR #{pr_number}",
        ]
    )
    return "\n".join(lines)


def parse_scope_expansion_child_body(body: object) -> ScopeExpansionChildIssueRecord | None:
    """Parse one child issue body and recover its immutable payload."""
    parsed = _parse_marker(body, SCOPE_EXPANSION_CHILD_MARKER_PREFIX)
    if parsed is None:
        return None
    digest, tail = parsed
    data = _lines_to_mapping(tail)
    if data is None:
        return None
    repository = data.get("repository")
    parent_issue = data.get("parent issue")
    pr_number = data.get("source pr")
    reviewed_head_value = data.get("reviewed head")
    title = data.get("title")
    reason = data.get("reason")
    source_path = data.get("source path")
    source_line = data.get("source line")
    required_paths = _parse_bullet_list(data.get("required paths"))
    acceptance_criteria = _parse_bullet_list(data.get("acceptance criteria"))
    child_issue_number = data.get("child issue")
    if (
        not isinstance(repository, str)
        or not isinstance(title, str)
        or not isinstance(reason, str)
        or not isinstance(source_path, str)
        or not isinstance(parent_issue, int)
        or not isinstance(pr_number, int)
        or not isinstance(source_line, int)
        or not isinstance(reviewed_head_value, str)
        or not isinstance(required_paths, tuple)
        or not isinstance(acceptance_criteria, tuple)
    ):
        return None
    reviewed_head_sha = reviewed_head_value
    if reviewed_head_sha.startswith("`") and reviewed_head_sha.endswith("`"):
        reviewed_head_sha = reviewed_head_sha[1:-1]
    if _FULL_SHA_RE.fullmatch(reviewed_head_sha) is None:
        return None
    expansion = ScopeExpansion(
        title=title,
        reason=reason,
        source_path=source_path.strip("`"),
        source_line=source_line,
        required_paths=required_paths,
        acceptance_criteria=acceptance_criteria,
    )
    if digest != scope_expansion_digest(repository, parent_issue, expansion):
        return None
    return ScopeExpansionChildIssueRecord(
        version=_VERSION,
        repository=repository,
        parent_issue=parent_issue,
        pr_number=pr_number,
        reviewed_head_sha=reviewed_head_sha,
        digest=digest,
        expansion=expansion,
        child_issue_number=child_issue_number if isinstance(child_issue_number, int) else None,
    )


def render_scope_expansion_lifecycle_comment(
    *,
    repository: str,
    parent_issue: int,
    pr_number: int,
    reviewed_head_sha: str,
    expansion: ScopeExpansion,
    state: ScopeExpansionLifecycleState,
    child_issue_number: int | None = None,
    merge_sha: str | None = None,
) -> str:
    """Render one durable source-PR lifecycle comment."""
    _positive_identifier(parent_issue, "parent_issue")
    _positive_identifier(pr_number, "pr_number")
    _full_sha(reviewed_head_sha, "reviewed_head_sha")
    digest = scope_expansion_digest(repository, parent_issue, expansion)
    lines = [
        f"<!-- hephaestus-scope-expansion-lifecycle:{digest} -->",
        "## Scope expansion lifecycle",
        f"Version: {_VERSION}",
        f"State: {state}",
        f"Repository: {repository.strip().lower()}",
        f"Parent issue: #{parent_issue}",
        f"Source PR: #{pr_number}",
        f"Reviewed head: `{reviewed_head_sha}`",
    ]
    if child_issue_number is not None:
        _positive_identifier(child_issue_number, "child_issue_number")
        lines.append(f"Child issue: #{child_issue_number}")
    if merge_sha is not None:
        _full_sha(merge_sha, "merge_sha")
        lines.append(f"Merge sha: `{merge_sha}`")
    return "\n".join(lines)


def parse_scope_expansion_lifecycle_comment(
    body: object,
) -> ScopeExpansionLifecycleRecord | None:
    """Parse one lifecycle comment body back into a durable record."""
    parsed = _parse_marker(body, SCOPE_EXPANSION_LIFECYCLE_MARKER_PREFIX)
    if parsed is None:
        return None
    digest, tail = parsed
    data = _lines_to_mapping(tail)
    if data is None:
        return None
    try:
        state = data["state"]
        repository = data["repository"]
        parent_issue = data["parent issue"]
        pr_number = data["source pr"]
        reviewed_head_sha = data["reviewed head"]
    except KeyError:
        return None
    child_issue_number = data.get("child issue")
    merge_sha = data.get("merge sha")
    if (
        not isinstance(state, str)
        or state not in {"pending-child", "pending-review", "blocked"}
        or not isinstance(repository, str)
        or not isinstance(parent_issue, int)
        or not isinstance(pr_number, int)
        or not isinstance(reviewed_head_sha, str)
        or _FULL_SHA_RE.fullmatch(reviewed_head_sha.strip("`")) is None
    ):
        return None
    if child_issue_number is not None and not isinstance(child_issue_number, int):
        return None
    if merge_sha is not None:
        if not isinstance(merge_sha, str) or _FULL_SHA_RE.fullmatch(merge_sha.strip("`")) is None:
            return None
        merge_sha = merge_sha.strip("`")
    return ScopeExpansionLifecycleRecord(
        version=_VERSION,
        state=state,  # type: ignore[arg-type]
        repository=repository,
        parent_issue=parent_issue,
        pr_number=pr_number,
        reviewed_head_sha=reviewed_head_sha.strip("`"),
        digest=digest,
        child_issue_number=child_issue_number,
        merge_sha=merge_sha,
    )


def render_scope_expansion_blocking_review(
    *,
    repository: str,
    parent_issue: int,
    pr_number: int,
    reviewed_head_sha: str,
    child_issue_number: int,
    expansion: ScopeExpansion,
) -> str:
    """Render the review body that records one child issue as blocking."""
    _positive_identifier(child_issue_number, "child_issue_number")
    _full_sha(reviewed_head_sha, "reviewed_head_sha")
    digest = scope_expansion_digest(repository, parent_issue, expansion)
    return "\n".join(
        [
            f"<!-- hephaestus-scope-expansion-blocking-review:{digest} -->",
            "Scope expansion child issue blocks this pull request.",
            f"Repository: {repository.strip().lower()}",
            f"Parent issue: #{parent_issue}",
            f"Source PR: #{pr_number}",
            f"Reviewed head: `{reviewed_head_sha}`",
            f"Child issue: #{child_issue_number}",
            f"Blocks PR #{pr_number}",
        ]
    )


def parse_scope_expansion_blocking_review(
    body: object,
) -> ScopeExpansionBlockingReviewRecord | None:
    """Parse a blocking-review body back into its immutable record."""
    parsed = _parse_marker(body, SCOPE_EXPANSION_BLOCKING_REVIEW_MARKER_PREFIX)
    if parsed is None:
        return None
    digest, tail = parsed
    data = _lines_to_mapping(tail)
    if data is None:
        return None
    try:
        repository = data["repository"]
        parent_issue = data["parent issue"]
        pr_number = data["source pr"]
        reviewed_head_sha = data["reviewed head"]
        child_issue_number = data["child issue"]
    except KeyError:
        return None
    if (
        not isinstance(repository, str)
        or not isinstance(parent_issue, int)
        or not isinstance(pr_number, int)
        or not isinstance(reviewed_head_sha, str)
        or _FULL_SHA_RE.fullmatch(reviewed_head_sha.strip("`")) is None
        or not isinstance(child_issue_number, int)
    ):
        return None
    return ScopeExpansionBlockingReviewRecord(
        version=_VERSION,
        repository=repository,
        parent_issue=parent_issue,
        pr_number=pr_number,
        reviewed_head_sha=reviewed_head_sha.strip("`"),
        child_issue_number=child_issue_number,
        digest=digest,
    )


def render_pending_retraction_projection(paths: tuple[str, ...]) -> str:
    """Render a bounded path projection for mixed audits that retain retractions."""
    return "\n".join([f"{SCOPE_EXPANSION_RETRACTION_MARKER_PREFIX} {json.dumps(paths)} -->"])


def _lines_to_mapping(lines: list[str]) -> dict[str, object] | None:
    """Parse strict key-value lines and bullet sections into one mapping."""
    data: dict[str, object] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if ": " in line:
            key, value = line.split(": ", 1)
            key = key.casefold()
            if key in data:
                return None
            if value.startswith("#") and value[1:].isdigit():
                data[key] = int(value[1:])
            elif value.isdigit():
                data[key] = int(value)
            elif (
                value.startswith("`")
                and value.endswith("`")
                and _FULL_SHA_RE.fullmatch(value[1:-1])
            ):
                data[key] = value[1:-1]
            else:
                data[key] = value
            index += 1
            continue
        if line.endswith(":"):
            key = line[:-1].casefold()
            if key in data:
                return None
            items: list[str] = []
            index += 1
            while index < len(lines) and lines[index].startswith("- "):
                items.append(lines[index][2:])
                index += 1
            data[key] = tuple(items)
            continue
        return None
    return data


def _parse_bullet_list(value: object) -> tuple[str, ...] | None:
    """Return a normalized tuple from one parsed bullet-list section."""
    if not isinstance(value, tuple) or not value:
        return None
    normalized = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if not text:
            return None
        if text.startswith("`") and text.endswith("`"):
            text = text[1:-1]
        normalized.append(text)
    return tuple(normalized)
