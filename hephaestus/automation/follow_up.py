"""Follow-up issue creation for issue implementation.

Policy (2026-05-10): one consolidated GitHub issue per implementation, sectioned
by category (core / security / safety / critical_bug). Follow-ups are tightly
scoped — see ``prompts.FOLLOW_UP_PROMPT`` for the rules. Out-of-scope items
that the model considered but rejected are returned to the caller so they can
be recorded in the PR body rather than filed as separate issues.

Public surface:

- ``parse_follow_up_response(text)`` — returns a typed result with
  ``follow_ups`` and ``rejected`` lists.
- ``run_follow_up_issues(...)`` — resumes the Claude session, parses the
  response, files at most one consolidated issue, and returns the parsed
  ``FollowUpResponse`` (or ``None`` if Claude failed). The rejected list is
  also persisted to ``state_dir/follow-up-rejected-{issue_number}.json``
  so callers that don't read the return value still have access.
- ``render_rejected_for_pr_body(rejected)`` — markdown helper for embedding
  the rejected list into a PR body.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .claude_timeouts import follow_up_claude_timeout
from .git_utils import run
from .github_api import gh_issue_comment, gh_issue_create
from .prompts import get_follow_up_prompt

logger = logging.getLogger(__name__)


_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"core", "security", "safety", "critical_bug"})
_MAX_FOLLOW_UPS = 3
_CATEGORY_LABELS: dict[str, list[str]] = {
    "core": ["follow-up", "core"],
    "security": ["follow-up", "security"],
    "safety": ["follow-up", "safety"],
    "critical_bug": ["follow-up", "bug"],
}
_SECTION_HEADINGS: dict[str, str] = {
    "core": "## Core library",
    "security": "## Security",
    "safety": "## Safety",
    "critical_bug": "## Critical bug",
}


@dataclass(frozen=True)
class FollowUpItem:
    """A single accepted follow-up item under one of the four categories."""

    category: str
    title: str
    body: str


@dataclass(frozen=True)
class RejectedItem:
    """A follow-up the model considered but excluded under the scope rules."""

    title: str
    reason: str


@dataclass(frozen=True)
class FollowUpResponse:
    """Parsed result of the follow-up Claude session."""

    follow_ups: list[FollowUpItem] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)


def _extract_outer_json_object(text: str) -> str | None:
    """Find and return the outermost JSON object ``{...}`` in *text*.

    Uses a balanced-brace scan so prose containing additional ``{`` or
    nested objects does not confuse the boundary detection. Carries the
    A5-07 lesson (originally applied to the array variant in the prior
    schema): a greedy regex over-consumes, a non-greedy regex stops too
    early inside nested structures.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from ``text``.

    Looks for a fenced ```json ... ``` block first, then falls back to a
    balanced-brace scan over the bare text. Returns ``None`` if nothing
    parseable is found.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else _extract_outer_json_object(text)
    if candidate is None:
        return None

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _classify_follow_up_entry(
    item: Any,
) -> tuple[FollowUpItem, None] | tuple[None, RejectedItem | None]:
    """Validate one ``follow_ups`` entry.

    Returns ``(item, None)`` if accepted, ``(None, RejectedItem)`` if demoted
    due to an invalid category, or ``(None, None)`` if the entry should be
    silently dropped (missing required fields).
    """
    if not isinstance(item, dict):
        return None, None
    category = item.get("category")
    title = item.get("title")
    body = item.get("body")
    if not isinstance(title, str) or not isinstance(body, str) or not title.strip():
        logger.warning("Skipping follow-up with missing title/body: %r", item)
        return None, None
    if category not in _ALLOWED_CATEGORIES:
        logger.warning("Demoting follow-up %r to rejected: invalid category %r", title, category)
        reason = f"Invalid category {category!r}; allowed values are {sorted(_ALLOWED_CATEGORIES)}."
        return None, RejectedItem(title=title, reason=reason)
    return FollowUpItem(category=category, title=title.strip(), body=body), None


def _parse_rejected_entry(item: Any) -> RejectedItem | None:
    """Validate one ``rejected`` entry, returning ``None`` if it should be dropped."""
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    reason = item.get("reason", "")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(reason, str):
        reason = str(reason)
    return RejectedItem(title=title.strip(), reason=reason.strip())


def parse_follow_up_response(text: str) -> FollowUpResponse:
    """Parse the sectioned follow-up schema from Claude's response.

    Tolerates fenced JSON, bare JSON, and silently drops malformed items
    (logged at WARNING). Items with categories outside the allowed set are
    demoted to the rejected list with a synthesised reason — the model is
    supposed to enforce categories itself, but defence in depth.
    """
    obj = _extract_json_object(text)
    if obj is None:
        logger.warning("No JSON object found in follow-up response")
        return FollowUpResponse()

    raw_follow_ups = obj.get("follow_ups", [])
    raw_rejected = obj.get("rejected", [])
    if not isinstance(raw_follow_ups, list):
        logger.warning("follow_ups was not a list; ignoring")
        raw_follow_ups = []
    if not isinstance(raw_rejected, list):
        logger.warning("rejected was not a list; ignoring")
        raw_rejected = []

    accepted: list[FollowUpItem] = []
    rejected: list[RejectedItem] = []

    for item in raw_follow_ups[:_MAX_FOLLOW_UPS]:
        good, demoted = _classify_follow_up_entry(item)
        if good is not None:
            accepted.append(good)
        elif demoted is not None:
            rejected.append(demoted)

    if len(raw_follow_ups) > _MAX_FOLLOW_UPS:
        logger.warning(
            "Follow-up cap exceeded: model returned %d items, kept first %d",
            len(raw_follow_ups),
            _MAX_FOLLOW_UPS,
        )

    for item in raw_rejected:
        parsed = _parse_rejected_entry(item)
        if parsed is not None:
            rejected.append(parsed)

    return FollowUpResponse(follow_ups=accepted, rejected=rejected)


def parse_follow_up_items(text: str) -> list[dict[str, Any]]:
    """Backward-compatible adapter returning a flat list of dicts.

    Pre-2026-05-10 callers expected ``[{"title", "body", "labels"}, ...]``.
    The new schema is structured differently, so this adapter projects
    accepted follow-ups into the legacy shape. Rejected items are NOT
    surfaced through this function — call ``parse_follow_up_response`` to
    see them.
    """
    response = parse_follow_up_response(text)
    return [
        {
            "title": item.title,
            "body": item.body,
            "labels": list(_CATEGORY_LABELS.get(item.category, ["follow-up"])),
        }
        for item in response.follow_ups
    ]


def _build_consolidated_body(
    response: FollowUpResponse,
    issue_number: int,
) -> str:
    """Render a single sectioned issue body covering all accepted follow-ups."""
    lines: list[str] = [
        f"_Consolidated follow-up from implementation of #{issue_number}._",
        "",
        (
            "Each section below lists scope-checked follow-up items discovered "
            "during implementation. Items are restricted to core library "
            "defects, security, safety hazards, or critical functional bugs."
        ),
        "",
    ]
    by_category: dict[str, list[FollowUpItem]] = {}
    for item in response.follow_ups:
        by_category.setdefault(item.category, []).append(item)

    for category in ("critical_bug", "safety", "security", "core"):
        if not by_category.get(category):
            continue
        lines.append(_SECTION_HEADINGS[category])
        lines.append("")
        for entry in by_category[category]:
            lines.append(f"### {entry.title}")
            lines.append("")
            lines.append(entry.body.strip())
            lines.append("")

    if response.rejected:
        lines.append("---")
        lines.append("")
        lines.append(
            "_The implementer also considered the items below and rejected "
            "them as out of scope; they are recorded in the PR body, not "
            "filed as separate issues._"
        )

    return "\n".join(lines).rstrip() + "\n"


def _consolidated_labels(response: FollowUpResponse) -> list[str]:
    """Pick labels for the consolidated issue based on which categories appear."""
    labels: set[str] = {"follow-up"}
    for item in response.follow_ups:
        labels.update(_CATEGORY_LABELS.get(item.category, []))
    return sorted(labels)


def _file_consolidated_issue(
    response: FollowUpResponse,
    issue_number: int,
    status_tracker: Any | None,
    slot_id: int | None,
    dry_run: bool,
) -> int | None:
    """File the single consolidated follow-up issue.

    Returns the new issue number on success, ``None`` if there was nothing
    to file or the call was suppressed by ``dry_run``. Failures are logged
    and swallowed — follow-up filing must never block the PR pipeline.
    """
    if not response.follow_ups:
        return None

    if slot_id is not None and status_tracker is not None:
        status_tracker.update_slot(
            slot_id, f"#{issue_number}: Filing 1 consolidated follow-up issue"
        )

    categories = sorted({i.category for i in response.follow_ups})
    title = (
        f"Follow-up from #{issue_number}: "
        f"{len(response.follow_ups)} item(s) ({', '.join(categories)})"
    )
    if len(title) > 80:
        title = f"Follow-up from #{issue_number} ({len(response.follow_ups)} items)"

    body = _build_consolidated_body(response, issue_number)
    labels = _consolidated_labels(response)

    if dry_run:
        logger.info(
            "[DRY RUN] Would create consolidated follow-up issue %r (parent #%d, %d items)",
            title,
            issue_number,
            len(response.follow_ups),
        )
        return None

    try:
        return gh_issue_create(title=title, body=body, labels=labels)
    except Exception as e:  # broad: GitHub API can fail in many ways; non-blocking
        logger.warning(
            "Failed to create consolidated follow-up issue for #%d: %s",
            issue_number,
            e,
        )
        return None


def _persist_rejected(
    rejected: list[RejectedItem],
    issue_number: int,
    state_dir: Path,
) -> None:
    """Write rejected items to a JSON file for offline inspection / PR-body rendering."""
    if not rejected:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"follow-up-rejected-{issue_number}.json"
    payload = [{"title": r.title, "reason": r.reason} for r in rejected]
    with contextlib.suppress(Exception):
        path.write_text(json.dumps(payload, indent=2) + "\n")


def run_follow_up_issues(
    session_id: str,
    worktree_path: Path,
    issue_number: int,
    state_dir: Path,
    status_tracker: Any | None = None,
    slot_id: int | None = None,
    dry_run: bool = False,
) -> FollowUpResponse | None:
    """Resume the implementation Claude session and file ONE consolidated follow-up issue.

    Returns the parsed ``FollowUpResponse`` so callers can render the rejected
    items into the PR body. Returns ``None`` if Claude failed or the response
    was unparseable — in that case the caller should not block the PR
    pipeline.

    Side effects:

    - Creates ``state_dir`` if missing.
    - Writes ``state_dir/follow-up-{issue_number}.log`` with the raw Claude output.
    - Writes ``state_dir/follow-up-rejected-{issue_number}.json`` with the
      rejected list.
    - Files at most ONE GitHub issue (the consolidated one), and posts a single
      summary comment on the parent issue when something was filed.
    - In ``dry_run`` mode, all GitHub side effects are suppressed.
    """
    state_dir.mkdir(parents=True, exist_ok=True)

    prompt_file = worktree_path / f".claude-followup-{issue_number}.md"
    prompt_file.write_text(get_follow_up_prompt(issue_number))

    try:
        result = run(
            [
                "claude",
                "--resume",
                session_id,
                str(prompt_file),
                "--output-format",
                "json",
            ],
            cwd=worktree_path,
            timeout=follow_up_claude_timeout(),
        )

        follow_up_log = state_dir / f"follow-up-{issue_number}.log"
        follow_up_log.write_text(result.stdout or "")

        try:
            data = json.loads(result.stdout)
            response_text = data.get("result", "")
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning("Could not parse follow-up response for issue #%d: %s", issue_number, e)
            return None

        response = parse_follow_up_response(response_text)
        _persist_rejected(response.rejected, issue_number, state_dir)

        if not response.follow_ups and not response.rejected:
            logger.info("No follow-up items identified for issue #%d", issue_number)
            return response

        new_issue = _file_consolidated_issue(
            response, issue_number, status_tracker, slot_id, dry_run=dry_run
        )

        if new_issue is not None:
            summary = (
                f"Filed consolidated follow-up issue #{new_issue} covering "
                f"{len(response.follow_ups)} item(s)"
            )
            if response.rejected:
                summary += (
                    f"; {len(response.rejected)} additional item(s) were "
                    "considered and rejected as out of scope (see PR body)."
                )
            try:
                gh_issue_comment(issue_number, summary)
                logger.info("Posted follow-up summary to issue #%d", issue_number)
            except Exception as e:  # non-critical summary post
                logger.warning("Failed to post follow-up summary: %s", e)

        logger.info(
            "Follow-up complete for #%d: filed=%s accepted=%d rejected=%d",
            issue_number,
            new_issue if new_issue is not None else "none",
            len(response.follow_ups),
            len(response.rejected),
        )
        return response

    except (
        Exception
    ) as e:  # broad: top-level boundary; follow-up failure must NEVER block the PR pipeline
        logger.warning("Follow-up issues failed for issue #%d: %s", issue_number, e)
        follow_up_log = state_dir / f"follow-up-{issue_number}.log"
        error_output = f"FAILED: {e}\n"
        if hasattr(e, "stdout"):
            error_output += f"\nSTDOUT:\n{e.stdout or ''}"
        if hasattr(e, "stderr"):
            error_output += f"\nSTDERR:\n{e.stderr or ''}"
        with contextlib.suppress(Exception):
            follow_up_log.write_text(error_output)
        return None
    finally:
        with contextlib.suppress(Exception):
            prompt_file.unlink()


def render_rejected_for_pr_body(rejected: list[RejectedItem]) -> str:
    """Render the rejected list as a markdown section suitable for inclusion in a PR body.

    Returns an empty string if there are no rejected items so the caller can
    unconditionally append the result.
    """
    if not rejected:
        return ""
    lines = [
        "",
        "## Considered & rejected follow-ups",
        "",
        (
            "_The implementer considered the items below during the follow-up "
            "scope check and rejected them under the policy "
            "(core / security / safety / critical_bug only)._"
        ),
        "",
    ]
    for item in rejected:
        lines.append(f"- **{item.title}** — {item.reason}".rstrip())
    return "\n".join(lines).rstrip() + "\n"
