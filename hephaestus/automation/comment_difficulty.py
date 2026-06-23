"""Classify PR review comments by difficulty and map to a model tier (#1083).

The in-loop address step fixes one review comment per sub-agent. To spend the
right amount of model capability on each, a cheap read-only classifier labels
every unresolved comment ``simple`` / ``medium`` / ``hard``; the label then
selects the fix sub-agent's model tier:

- ``simple`` → Haiku (typo, rename, doc tweak, one-line guard)
- ``medium`` → Sonnet (localized logic change, small refactor, edge case)
- ``hard``   → Opus (cross-cutting design change, tricky correctness/security)

The classifier is a separate sub-agent (not the reviewer and not the fixer) so
the difficulty judgment is independent of both. Classification failures degrade
gracefully to ``medium`` (the safe middle tier) — a misclassification must never
block the address step.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    is_codex,
    run_agent_text,
    run_codex_text,
    uses_direct_agent_runner,
)

from ._review_utils import parse_json_block
from .claude_invoke import invoke_claude_with_session
from .claude_models import HAIKU, OPUS, SONNET, advise_model
from .claude_timeouts import advise_claude_timeout
from .git_utils import get_repo_slug
from .prompts import get_comment_difficulty_prompt
from .session_naming import AGENT_COMMENT_CLASSIFIER

logger = logging.getLogger(__name__)

#: Allowed difficulty labels, in ascending order of effort.
DIFFICULTIES = ("simple", "medium", "hard")

#: Difficulty → model tier. Unknown labels fall back to the middle tier.
_DIFFICULTY_MODEL = {
    "simple": HAIKU,
    "medium": SONNET,
    "hard": OPUS,
}

#: Default applied when the classifier omits or mis-labels a thread.
_DEFAULT_DIFFICULTY = "medium"


def model_for_difficulty(difficulty: str) -> str:
    """Return the model ID for a difficulty label.

    Unknown labels map to the middle (Sonnet) tier so a bad classification
    never silently downgrades a hard fix to Haiku.
    """
    return _DIFFICULTY_MODEL.get(difficulty, SONNET)


#: Max length of the (untrusted) description excerpt in a todo line.
_DESC_MAX = 200


def format_todo_line(thread: dict[str, Any], difficulty: str) -> str:
    """Render one thread as ``@ <file> Line <#> - <difficulty> - <description>``.

    The description is a sanitized one-line excerpt of the comment body. Because
    the body is untrusted GitHub content, it is reduced to a single physical line
    (no newlines/carriage returns can forge extra prompt instructions, #1085 C4)
    and truncated to keep it from dominating the prompt. The full body is still
    available to the coordinator inside the fenced threads JSON. A null/absent
    line renders as ``Line ?``.
    """
    path = thread.get("path") or "__general__"
    line = thread.get("line")
    line_str = str(line) if isinstance(line, int) else "?"
    body = (thread.get("body") or "").strip()
    if body:
        # First physical line only, with any stray CR and control chars stripped.
        first = body.splitlines()[0]
        description = "".join(ch for ch in first if ch == " " or ch.isprintable()).strip()
        description = description or "(no description)"
        if len(description) > _DESC_MAX:
            description = description[:_DESC_MAX] + "…"
    else:
        description = "(no description)"
    return f"@ {path} Line {line_str} - {difficulty} - {description}"


def _run_classifier_session(
    *,
    threads: list[dict[str, Any]],
    agent: str,
    issue_number: int,
    worktree_path: Path,
    repo_root: Path,
    state_dir: Path,
) -> dict[str, str]:
    """Run the read-only classifier sub-agent; return ``{thread_id: difficulty}``.

    On any agent/parse failure returns an empty dict — the caller then defaults
    every thread to ``medium``.
    """
    comments_json = json.dumps(
        [
            {
                "thread_id": t["id"],
                "path": t.get("path", ""),
                "line": t.get("line"),
                "body": t.get("body", ""),
            }
            for t in threads
        ]
    )
    prompt = get_comment_difficulty_prompt(
        issue_number=issue_number,
        comments_json=comments_json,
    )
    log_file = state_dir / f"comment-difficulty-{issue_number}.log"
    try:
        if is_codex(agent):
            result = run_codex_text(
                prompt,
                cwd=worktree_path,
                timeout=advise_claude_timeout(),
                sandbox="read-only",
            )
            log_file.write_text(result.stdout or "")
            parsed = parse_json_block(result.stdout or "")
        elif uses_direct_agent_runner(agent):
            result = run_agent_text(
                agent=agent,
                prompt=prompt,
                cwd=worktree_path,
                timeout=advise_claude_timeout(),
                model=direct_agent_model(agent, "HEPH_ADVISE_MODEL"),
                sandbox="read-only",
            )
            log_file.write_text(result.stdout or "")
            parsed = parse_json_block(result.stdout or "")
        else:
            stdout, _ = invoke_claude_with_session(
                repo=get_repo_slug(repo_root),
                issue=issue_number,
                agent=AGENT_COMMENT_CLASSIFIER,
                prompt=prompt,
                model=advise_model(),
                cwd=worktree_path,
                timeout=advise_claude_timeout(),
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            log_file.write_text(stdout or "")
            try:
                data = json.loads(stdout or "{}")
                response_text: str = data.get("result", stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response_text = stdout or ""
            parsed = parse_json_block(response_text)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "Issue #%s: comment-difficulty classifier failed (%s); defaulting to medium",
            issue_number,
            exc,
        )
        return {}

    classifications = parsed.get("classifications", {})
    if not isinstance(classifications, dict):
        return {}
    # Keep only valid string→difficulty entries.
    return {
        str(tid): str(diff) for tid, diff in classifications.items() if str(diff) in DIFFICULTIES
    }


def classify_comments(
    *,
    threads: list[dict[str, Any]],
    agent: str,
    issue_number: int,
    worktree_path: Path,
    repo_root: Path,
    state_dir: Path,
    dry_run: bool = False,
) -> dict[str, str]:
    """Classify each thread's difficulty; return ``{thread_id: difficulty}``.

    Every thread in *threads* is present in the result. Threads the classifier
    omitted or mis-labeled default to ``medium`` so the caller always has a tier
    for each. Returns ``{}`` for an empty thread list and never raises.
    """
    if not threads:
        return {}
    if dry_run:
        return {t["id"]: _DEFAULT_DIFFICULTY for t in threads}

    classified = _run_classifier_session(
        threads=threads,
        agent=agent,
        issue_number=issue_number,
        worktree_path=worktree_path,
        repo_root=repo_root,
        state_dir=state_dir,
    )
    return {t["id"]: classified.get(t["id"], _DEFAULT_DIFFICULTY) for t in threads}
