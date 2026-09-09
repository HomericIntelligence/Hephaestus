"""Pull-request diff position helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import hephaestus.automation.github_api as _api

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

ReviewAnchorCorrectionReason = Literal["anchor_not_in_reviewed_diff", "reviewed_diff_unavailable"]


@dataclass(frozen=True)
class ReviewAnchorCorrection:
    """Preserve one finding that needs a different review anchor."""

    finding: dict[str, Any]
    path: str
    line: int | None
    side: str
    reason: ReviewAnchorCorrectionReason

    def __post_init__(self) -> None:
        """Validate the bounded correction fields."""
        if not isinstance(self.finding, dict):
            raise ValueError("finding must be a dictionary")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path must be a non-empty string")
        if self.line is not None and (
            isinstance(self.line, bool) or not isinstance(self.line, int)
        ):
            raise ValueError("line must be an integer or None")
        if not isinstance(self.side, str) or not self.side:
            raise ValueError("side must be a non-empty string")
        if not isinstance(self.reason, str) or self.reason not in {
            "anchor_not_in_reviewed_diff",
            "reviewed_diff_unavailable",
        }:
            raise ValueError("reason must be a supported anchor-correction reason")


@dataclass(frozen=True)
class ReviewCommentValidation:
    """Separate valid review comments from findings that need correction."""

    valid: tuple[dict[str, Any], ...]
    corrections: tuple[ReviewAnchorCorrection, ...]


def _valid_review_positions(diff_text: str) -> dict[str, set[tuple[int, str]]]:
    """Map each changed file to the ``(line, side)`` positions GitHub will accept.

    GitHub's review API rejects (HTTP 422) any inline comment whose ``line``/
    ``side`` does not fall on a line present in the PR diff. A ``RIGHT`` comment
    must target an added (``+``) or context (`` ``) line in the new file; a
    ``LEFT`` comment must target a removed (``-``) or context line in the old
    file. This parses the unified diff once into the set of accepted positions.

    Args:
        diff_text: Unified diff (``gh pr diff <n>`` output).

    Returns:
        ``{path: {(line_number, side), ...}}`` for every changed file.

    """
    positions: dict[str, set[tuple[int, str]]] = {}
    current_path: str | None = None
    old_line = 0
    new_line = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            # ``+++ b/path`` (or ``+++ /dev/null``); strip the ``b/`` prefix.
            target = raw[4:].strip()
            if target == "/dev/null":
                current_path = None
            else:
                current_path = target[2:] if target.startswith("b/") else target
                positions.setdefault(current_path, set())
            continue
        if raw.startswith("--- "):
            # Old-file header; new-file header (+++) sets the path.
            continue

        header = _HUNK_HEADER_RE.match(raw)
        if header:
            old_line = int(header.group(1))
            new_line = int(header.group(2))
            continue

        if current_path is None or not raw:
            continue

        marker = raw[0]
        if marker == "+":
            positions[current_path].add((new_line, "RIGHT"))
            new_line += 1
        elif marker == "-":
            positions[current_path].add((old_line, "LEFT"))
            old_line += 1
        elif marker == " ":
            # Context line is valid on both sides.
            positions[current_path].add((new_line, "RIGHT"))
            positions[current_path].add((old_line, "LEFT"))
            old_line += 1
            new_line += 1
        # Any other marker (e.g. ``\`` for "No newline") is ignored.

    return positions


def _validate_comments_to_diff(
    comments: list[dict[str, Any]], diff_text: str, *, fail_open_empty: bool = False
) -> ReviewCommentValidation:
    """Validate every comment against one immutable unified diff.

    Args:
        comments: Candidate inline review comments.
        diff_text: The diff that the reviewer inspected.
        fail_open_empty: Keep the legacy behavior for callers that cannot
            provide a diff. The pipeline passes a bound diff and does not use
            this compatibility mode.

    Returns:
        Valid comments and typed correction records for invalid comments.

    """
    if not diff_text.strip() and fail_open_empty:
        return ReviewCommentValidation(tuple(comments), ())

    valid_positions = _valid_review_positions(diff_text)
    valid: list[dict[str, Any]] = []
    corrections: list[ReviewAnchorCorrection] = []
    for comment in comments:
        path_value = comment.get("path")
        path = path_value.strip() if isinstance(path_value, str) else str(path_value or "")
        line_value = comment.get("line")
        line = (
            line_value if isinstance(line_value, int) and not isinstance(line_value, bool) else None
        )
        side_value = comment.get("side", "RIGHT")
        side = side_value if isinstance(side_value, str) else str(side_value or "")
        if path in valid_positions and (line, side) in valid_positions[path]:
            valid.append(comment)
            continue
        corrections.append(
            ReviewAnchorCorrection(
                finding=dict(comment),
                path=path,
                line=line,
                side=side,
                reason=(
                    "reviewed_diff_unavailable"
                    if not diff_text.strip()
                    else "anchor_not_in_reviewed_diff"
                ),
            )
        )
        _api.logger.warning(
            "Preserving review finding on %s:%s (%s) for anchor correction: %s",
            path,
            line_value,
            side,
            corrections[-1].reason,
        )
    return ReviewCommentValidation(tuple(valid), tuple(corrections))


def _filter_comments_to_diff(
    comments: list[dict[str, Any]], diff_text: str
) -> list[dict[str, Any]]:
    """Return valid comments for callers that use the legacy list contract.

    The pipeline uses :func:`_validate_comments_to_diff` so it can preserve
    invalid findings in typed correction records. This wrapper keeps the
    historical list-only result for other callers.

    Fails open: if ``diff_text`` is empty (the diff could not be fetched), the
    comments are returned unchanged — losing a comment because the diff was
    unavailable would be worse than a possible 422.

    Args:
        comments: Inline comment dicts with ``path``/``line``/``side``/``body``.
        diff_text: Unified diff to validate against.

    Returns:
        The subset of ``comments`` that target a line present in the diff.

    """
    return list(_validate_comments_to_diff(comments, diff_text, fail_open_empty=True).valid)
