"""Classify review comments by difficulty without selecting a model."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_text,
    uses_direct_agent_runner,
)
from hephaestus.io.utils import write_secure

from ._review_utils import log_file_path, parse_json_block
from .agent_config import DEFAULT_AGENT_TIMEOUT
from .claude_invoke import invoke_claude_with_session
from .git_utils import get_repo_slug
from .prompts import get_comment_difficulty_prompt
from .session_naming import AGENT_COMMENT_CLASSIFIER

logger = logging.getLogger(__name__)

#: Allowed difficulty labels, in ascending order of effort.
DIFFICULTIES = ("simple", "medium", "hard")

#: Default when the classifier omits a thread or returns an unknown label.
_DEFAULT_DIFFICULTY = "medium"


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
    advise_timeout: int = DEFAULT_AGENT_TIMEOUT,
    model: str = "",
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
    log_file = log_file_path(state_dir, "comment-difficulty", issue_number)
    try:
        if uses_direct_agent_runner(agent):
            result = run_agent_text(
                agent=agent,
                prompt=prompt,
                cwd=worktree_path,
                timeout=advise_timeout,
                execution_request=ExecutionRequest(
                    AgentRole.PR_REVIEWER,
                    AgentOperation.COMMENT_CLASSIFY,
                    SessionLifecycle.ONE_SHOT,
                ),
                model=direct_agent_model(agent, model_value=model),
                sandbox="read-only",
            )
            write_secure(log_file, result.stdout or "")
            parsed = parse_json_block(result.stdout or "")
        else:
            stdout, _ = invoke_claude_with_session(
                repo=get_repo_slug(repo_root),
                issue=issue_number,
                agent=AGENT_COMMENT_CLASSIFIER,
                prompt=prompt,
                model=model,
                cwd=worktree_path,
                timeout=advise_timeout,
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            write_secure(log_file, stdout or "")
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
    advise_timeout: int = DEFAULT_AGENT_TIMEOUT,
    model: str = "",
) -> dict[str, str]:
    """Classify each thread's difficulty; return ``{thread_id: difficulty}``.

    Every thread in *threads* is present in the result. Threads the classifier
    omitted or mis-labeled default to ``medium`` so the caller always has a label
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
        advise_timeout=advise_timeout,
        model=model,
    )
    return {t["id"]: classified.get(t["id"], _DEFAULT_DIFFICULTY) for t in threads}
