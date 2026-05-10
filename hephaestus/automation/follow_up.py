"""Follow-up issue creation functions for issue implementation.

Provides:
- Parsing follow-up items from Claude JSON responses
- Creating follow-up GitHub issues after implementation
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .claude_timeouts import follow_up_claude_timeout
from .git_utils import run
from .github_api import gh_issue_comment, gh_issue_create
from .issue_dedup import extract_new_info, find_duplicate_open_issue
from .prompts import get_follow_up_prompt

logger = logging.getLogger(__name__)


def _extract_outer_json_array(text: str) -> str | None:
    """Find and return the outermost JSON array ``[...]`` in *text*.

    Uses a balanced-bracket scan to avoid the greedy/non-greedy pitfall of
    regex-based extraction, which either over-consumes (greedy ``.*``) or
    stops too early inside nested structures (non-greedy ``.*?``).

    Args:
        text: String that may contain a JSON array.

    Returns:
        The first outermost ``[...]`` substring, or ``None`` if not found.

    """
    start = text.find("[")
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
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_follow_up_items(text: str) -> list[dict[str, Any]]:
    """Parse follow-up items from Claude's JSON response.

    Args:
        text: Claude's response text (may contain JSON in code blocks)

    Returns:
        List of follow-up item dictionaries with title, body, labels

    """
    # Try to extract JSON from code blocks or bare JSON.
    # Code-block extraction uses a non-greedy inner match to stop at the
    # first closing fence.  The bare-JSON fallback previously used a greedy
    # `.*` which would over-consume when multiple arrays were present.
    # We now use a balanced-bracket scanner to find the outermost `[...]`
    # block reliably (A5-07).
    json_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        json_str = _extract_outer_json_array(text)
        if json_str is None:
            logger.warning("No JSON array found in follow-up response")
            return []

    try:
        items = json.loads(json_str)
        if not isinstance(items, list):
            logger.warning("Follow-up response is not a JSON array")
            return []

        # Validate and filter items
        valid_items = []
        for item in items[:5]:  # Cap at 5
            if not isinstance(item, dict):
                continue
            if "title" not in item or "body" not in item:
                logger.warning(f"Skipping follow-up item missing required fields: {item}")
                continue

            # Ensure labels is a list
            if "labels" not in item or not isinstance(item["labels"], list):
                item["labels"] = []

            valid_items.append(item)

        return valid_items

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse follow-up JSON: {e}")
        return []


def _create_follow_up_issues(
    items: list[dict[str, Any]],
    issue_number: int,
    status_tracker: Any | None,
    slot_id: int | None,
    dry_run: bool = False,
) -> list[int]:
    """Create GitHub issues from follow-up item list.

    Args:
        items: List of follow-up item dicts with title, body, and optional labels.
        issue_number: Parent issue number (for cross-references and status labels).
        status_tracker: Optional StatusTracker for slot updates.
        slot_id: Worker slot ID for status updates.
        dry_run: If True, log what would be filed without calling
            ``gh_issue_create`` or ``gh_issue_comment``. Defence-in-depth: the
            implementer phase already short-circuits the entire post-impl
            block under dry-run, but this guards future callers.

    Returns:
        List of created issue numbers (empty in dry-run).

    """
    created_issues = []
    for i, item in enumerate(items, 1):
        try:
            if slot_id is not None and status_tracker is not None:
                status_tracker.update_slot(
                    slot_id, f"#{issue_number}: Creating follow-up {i}/{len(items)}"
                )

            # Dedup: skip filing if a near-duplicate open issue already exists.
            # When found, post a comment on the existing issue with any new
            # information — but only if the new body actually adds something.
            duplicate = find_duplicate_open_issue(item["title"], item["body"])
            if duplicate is not None:
                new_info = extract_new_info(item["body"], duplicate.body)
                if new_info:
                    update_comment = (
                        f"Additional context from #{issue_number} "
                        f"(would have been a separate issue, "
                        f"deduplicated against this one):\n\n{new_info}"
                    )
                    if dry_run:
                        logger.info(
                            "[DRY RUN] Would update duplicate #%d with new context "
                            "from #%d (skipped duplicate %r)",
                            duplicate.number,
                            issue_number,
                            item["title"],
                        )
                    else:
                        try:
                            gh_issue_comment(duplicate.number, update_comment)
                            logger.info(
                                f"Updated existing issue #{duplicate.number} with new "
                                f"context from #{issue_number} "
                                f"(skipped duplicate '{item['title']}')"
                            )
                        except Exception as e:  # comment is best-effort
                            logger.warning(
                                f"Failed to comment on duplicate #{duplicate.number}: {e}"
                            )
                else:
                    logger.info(
                        f"Skipped pure-duplicate follow-up '{item['title']}' "
                        f"(matches existing #{duplicate.number}, no new info)"
                    )
                time.sleep(1)
                continue

            body_with_ref = f"{item['body']}\n\n_Follow-up from #{issue_number}_"
            if dry_run:
                logger.info(
                    "[DRY RUN] Would create follow-up issue %r (parent #%d)",
                    item["title"],
                    issue_number,
                )
            else:
                new_issue_num = gh_issue_create(
                    title=item["title"],
                    body=body_with_ref,
                    labels=item.get("labels"),
                )
                created_issues.append(new_issue_num)
            time.sleep(1)
        except (
            Exception
        ) as e:  # broad catch: GitHub API can fail in many ways; continue with others
            logger.warning(f"Failed to create follow-up issue '{item['title']}': {e}")
    return created_issues


def run_follow_up_issues(
    session_id: str,
    worktree_path: Path,
    issue_number: int,
    state_dir: Path,
    status_tracker: Any | None = None,
    slot_id: int | None = None,
    dry_run: bool = False,
) -> None:
    """Resume Claude session to identify and file follow-up issues.

    Args:
        session_id: Claude session ID to resume
        worktree_path: Path to git worktree
        issue_number: Parent issue number
        state_dir: Directory for state/log files
        status_tracker: StatusTracker instance for slot updates (optional)
        slot_id: Worker slot ID for status updates
        dry_run: If True, run Claude analysis but suppress
            ``gh_issue_create``/``gh_issue_comment``. Defence-in-depth (the
            implementer-phase caller already short-circuits in dry-run).

    """
    state_dir.mkdir(parents=True, exist_ok=True)

    # Write follow-up prompt to temp file in worktree
    prompt_file = worktree_path / f".claude-followup-{issue_number}.md"
    prompt_file.write_text(get_follow_up_prompt(issue_number))

    try:
        # Resume session and get follow-up items
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

        # Save successful output to log file
        follow_up_log = state_dir / f"follow-up-{issue_number}.log"
        follow_up_log.write_text(result.stdout or "")

        # Parse JSON output
        try:
            data = json.loads(result.stdout)
            response_text = data.get("result", "")
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"Could not parse follow-up response for issue #{issue_number}: {e}")
            return

        # Extract follow-up items
        items = parse_follow_up_items(response_text)

        if not items:
            logger.info(f"No follow-up items identified for issue #{issue_number}")
            return

        created_issues = _create_follow_up_issues(
            items, issue_number, status_tracker, slot_id, dry_run=dry_run
        )

        # Post summary comment on parent issue (suppressed in dry-run)
        if created_issues:
            summary = f"Created {len(created_issues)} follow-up issue(s): " + ", ".join(
                f"#{num}" for num in created_issues
            )
            if dry_run:
                logger.info("[DRY RUN] Would post follow-up summary to #%d", issue_number)
            else:
                try:
                    gh_issue_comment(issue_number, summary)
                    logger.info(f"Posted follow-up summary to issue #{issue_number}")
                except Exception as e:  # broad catch: GitHub API call; non-critical summary post
                    logger.warning(f"Failed to post follow-up summary: {e}")

        logger.info(
            f"Follow-up issues completed for #{issue_number}: created {len(created_issues)}"
        )

    except (
        Exception
    ) as e:  # broad catch: top-level follow-up boundary; non-blocking, must not propagate
        logger.warning(f"Follow-up issues failed for issue #{issue_number}: {e}")

        # Save failure output to log file
        follow_up_log = state_dir / f"follow-up-{issue_number}.log"
        error_output = f"FAILED: {e}\n"
        if hasattr(e, "stdout"):
            error_output += f"\nSTDOUT:\n{e.stdout or ''}"
        if hasattr(e, "stderr"):
            error_output += f"\nSTDERR:\n{e.stderr or ''}"
        follow_up_log.write_text(error_output)

        # Non-blocking: never re-raise
    finally:
        # Clean up temp file
        with contextlib.suppress(Exception):
            prompt_file.unlink()
