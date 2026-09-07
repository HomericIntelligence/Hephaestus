"""Git commit operations that do not depend on GitHub product services."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.utils.git import git_config_get

from .commit_paths import (
    SECRET_FILE_EXTENSIONS as SECRET_FILE_EXTENSIONS,
    SECRET_FILE_NAMES as SECRET_FILE_NAMES,
    CommitPaths,
    is_bounded_commit_paths,
    parse_porcelain_status,
    reject_filtered_path_shape_changes,
    select_commit_paths,
)
from .commit_policy import ALLOWED_CONVENTIONAL_TYPES, normalize_conventional_type
from .git_runtime import issue_ref, run
from .prompts._shared import fence_content

logger = logging.getLogger(__name__)

_COMMIT_MANIFEST_MAX_PATHS = 512
_COMMIT_MANIFEST_MAX_BYTES = 64 * 1024
DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT = 1200
COMMIT_ISSUE_TITLE_MAX_BYTES = 1024
COMMIT_ISSUE_BODY_MAX_BYTES = 256 * 1024
_RESERVED_MESSAGE_LINE = re.compile(
    r"^\s*(?:Closes\s+#\d+|Implemented-By:|Co-Authored-By:)",
    re.IGNORECASE,
)
_AGENT_COMMIT_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex",
    "pi": "Pi",
    "opencode": "OpenCode-AI",
}
_AGENT_PROVENANCE = {
    "claude": "Claude Code",
    "codex": "Codex",
    "pi": "Pi",
    "opencode": "OpenCode",
}
_FALLBACK_AGENT_COMMIT_EMAIL = "noreply@hephaestus.invalid"

CommitMessageAgent = Callable[[int, str, Path, str, int, str, Path | None], str]

# Keep the established commit-policy helper names while this operation moves
# out of the pull-request product facade.
_CommitPaths = CommitPaths
_parse_porcelain_status = parse_porcelain_status
_select_commit_paths = select_commit_paths


@dataclass(frozen=True)
class CommitIssueMetadata:
    """Immutable issue text captured before a Git worker job is enqueued."""

    number: int
    title: str
    body: str

    def __post_init__(self) -> None:
        """Reject incomplete metadata before a commit operation starts."""
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number <= 0:
            raise ValueError("commit issue number must be a positive integer")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("commit issue title is unavailable")
        if not isinstance(self.body, str):
            raise ValueError("commit issue body is unavailable")
        if len(self.title.encode("utf-8")) > COMMIT_ISSUE_TITLE_MAX_BYTES:
            raise ValueError("commit issue title exceeds its UTF-8 limit")
        if len(self.body.encode("utf-8")) > COMMIT_ISSUE_BODY_MAX_BYTES:
            raise ValueError("commit issue body exceeds its UTF-8 limit")


@dataclass(frozen=True)
class _CommitMessageParts:
    """Agent-proposed commit message content before policy trailers."""

    subject: str
    body: str


def _git_timeout_kw(timeout: int | None) -> dict[str, Any]:
    """Return a ``run`` argument only when a Git timeout was provided."""
    return {} if timeout is None else {"timeout": timeout}


def _single_line(value: object, *, fallback: str, max_len: int = 120) -> str:
    """Normalize message text into one non-empty line."""
    text = str(value or "").strip().splitlines()[0].strip() if value else ""
    return (text or fallback)[:max_len].rstrip()


def _strip_reserved_lines(text: str) -> str:
    """Remove policy lines that the host must create."""
    return "\n".join(
        line.rstrip() for line in text.splitlines() if not _RESERVED_MESSAGE_LINE.match(line)
    ).strip()


def _message_text(value: object) -> str:
    """Normalize an agent JSON string or list field into Markdown text."""
    if isinstance(value, list):
        cleaned = [str(item).strip().lstrip("- ").strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in cleaned)
    return value.strip() if isinstance(value, str) else ""


def _parse_agent_json(text: str) -> dict[str, Any] | None:
    """Parse one JSON object from raw agent output."""
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _git_output(
    worktree_path: Path,
    args: list[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Return best-effort Git output for commit-message context."""
    try:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "check": False,
            **_git_timeout_kw(timeout),
        }
        if env is not None:
            kwargs["env"] = env
        result = run(["git", *args], cwd=worktree_path, **kwargs)
    except Exception as exc:
        logger.debug("Could not collect Git message context for %s: %s", args, exc)
        return ""
    return (result.stdout or "").strip()


def _staged_change_context(
    worktree_path: Path,
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return staged changed files and a diff statistic."""
    return (
        _git_output(
            worktree_path,
            ["diff", "--no-ext-diff", "--no-textconv", "--cached", "--name-status"],
            timeout=timeout,
            env=env,
        ),
        _git_output(
            worktree_path,
            ["diff", "--no-ext-diff", "--no-textconv", "--cached", "--stat"],
            timeout=timeout,
            env=env,
        ),
    )


def _commit_message_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    changed_files: str,
    diff_stat: str,
) -> str:
    """Build the read-only commit-message prompt."""
    fenced = fence_content()
    return PromptCatalog.current().render(
        "pr_management/commit_message.j2",
        allowed_types=", ".join(sorted(ALLOWED_CONVENTIONAL_TYPES)),
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body or "(empty)"),
        changed_files_block=fenced.fence("CHANGED_FILES", changed_files or "(none reported)"),
        diff_stat_block=fenced.fence("DIFF_STAT", diff_stat or "(none reported)"),
        untrusted_notice=fenced.untrusted_notice,
    )


def _invoke_git_message_agent(
    *,
    issue_number: int,
    prompt: str,
    worktree_path: Path,
    agent: str,
    timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    model_override: str | None = None,
    pi_dir: Path | None = None,
    claude_message_agent: CommitMessageAgent | None = None,
) -> str:
    """Run the lightweight commit-message agent in a read-only session."""
    model = model_override or ""
    if claude_message_agent is None:
        raise RuntimeError("commit-message agent is unavailable")
    return claude_message_agent(
        issue_number,
        prompt,
        worktree_path,
        agent,
        timeout,
        model,
        pi_dir,
    )


def _agentic_commit_email() -> str:
    """Return the operator email for the agent co-author trailer."""
    return git_config_get("user.email", global_=True) or _FALLBACK_AGENT_COMMIT_EMAIL


def _format_commit_message(
    *,
    metadata: CommitIssueMetadata,
    agent: str,
    subject: str,
    body: str,
    model: str | None = None,
) -> str:
    """Render a commit message with host-owned policy trailers."""
    coauthor_name = _AGENT_COMMIT_NAMES.get(agent, _AGENT_COMMIT_NAMES["claude"])
    provenance = (
        model or _AGENT_PROVENANCE["claude"]
        if agent == "claude"
        else _AGENT_PROVENANCE.get(agent, agent)
    )
    clean_body = _strip_reserved_lines(body)
    body_block = f"\n\n{clean_body}" if clean_body else ""
    return f"""{subject}{body_block}

Closes #{metadata.number}

Implemented-By: {provenance}
Co-Authored-By: {coauthor_name} <{_agentic_commit_email()}>
"""


def _generate_commit_message(
    metadata: CommitIssueMetadata,
    worktree_path: Path,
    agent: str,
    *,
    git_message_timeout: int,
    git_timeout: int | None,
    agent_model: str | None,
    pi_dir: Path | None,
    git_env: dict[str, str] | None,
    claude_message_agent: CommitMessageAgent | None = None,
) -> str:
    """Generate a commit message with a deterministic fallback."""
    changed_files, diff_stat = _staged_change_context(
        worktree_path,
        timeout=git_timeout,
        env=git_env,
    )
    prompt = _commit_message_prompt(
        issue_number=metadata.number,
        issue_title=metadata.title,
        issue_body=metadata.body,
        changed_files=changed_files,
        diff_stat=diff_stat,
    )
    try:
        raw = _invoke_git_message_agent(
            issue_number=metadata.number,
            prompt=prompt,
            worktree_path=worktree_path,
            agent=agent,
            timeout=git_message_timeout,
            model_override=agent_model,
            pi_dir=pi_dir,
            claude_message_agent=claude_message_agent,
        )
        data = _parse_agent_json(raw)
        if data is None:
            raise ValueError("message agent returned no JSON object")
        subject = normalize_conventional_type(
            _single_line(
                data.get("subject"),
                fallback=f"feat: Implement #{metadata.number}",
                max_len=120,
            )
        )
        return _format_commit_message(
            metadata=metadata,
            agent=agent,
            subject=subject,
            body=_message_text(data.get("body")),
            model=agent_model,
        )
    except Exception as exc:
        logger.warning(
            "Commit-message agent failed for %s; using fallback message (%s)",
            issue_ref(metadata.number),
            exc,
        )
        return _format_commit_message(
            metadata=metadata,
            agent=agent,
            subject=f"feat: Implement #{metadata.number}",
            body=metadata.title,
            model=agent_model,
        )


def _read_porcelain_status(
    worktree_path: Path,
    git_timeout: int | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Return stable NUL-delimited porcelain-v1 worktree status."""
    try:
        kwargs: dict[str, Any] = {"capture_output": True, **_git_timeout_kw(git_timeout)}
        if env is not None:
            kwargs["env"] = env
        result = run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            ],
            cwd=worktree_path,
            **kwargs,
        )
    except UnicodeDecodeError as exc:
        raise RuntimeError("Could not decode git status --porcelain=v1 -z output") from exc
    return result.stdout or ""


def _stage_commit_paths(
    paths: CommitPaths,
    worktree_path: Path,
    git_timeout: int | None,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Stage only the selected paths."""
    kwargs: dict[str, Any] = _git_timeout_kw(git_timeout)
    if env is not None:
        kwargs["env"] = env
    run(["git", "read-tree", "HEAD"], cwd=worktree_path, **kwargs)
    with tempfile.TemporaryDirectory(prefix="hephaestus-commit-pathspec-") as temporary:
        temporary_root = Path(temporary)
        if paths.update_paths:
            pathspec = temporary_root / "update-paths"
            pathspec.write_bytes(
                b"\0".join(os.fsencode(path) for path in paths.update_paths) + b"\0"
            )
            run(
                [
                    "git",
                    "--literal-pathspecs",
                    "rm",
                    "-r",
                    "-f",
                    "--cached",
                    "--ignore-unmatch",
                    f"--pathspec-from-file={pathspec}",
                    "--pathspec-file-nul",
                ],
                cwd=worktree_path,
                **kwargs,
            )
        if paths.add_paths:
            pathspec = temporary_root / "add-paths"
            pathspec.write_bytes(b"\0".join(os.fsencode(path) for path in paths.add_paths) + b"\0")
            run(
                [
                    "git",
                    "--literal-pathspecs",
                    "add",
                    "-A",
                    f"--pathspec-from-file={pathspec}",
                    "--pathspec-file-nul",
                ],
                cwd=worktree_path,
                **kwargs,
            )


def _clear_local_committer_identity(worktree_path: Path, git_timeout: int | None) -> None:
    """Remove worktree-local committer identity overrides."""
    for key in ("user.email", "user.name"):
        run(
            ["git", "config", "--unset", "--local", key],
            cwd=worktree_path,
            check=False,
            log_errors=False,
            **_git_timeout_kw(git_timeout),
        )


def _commit_with_signature(
    commit_message: str,
    worktree_path: Path,
    git_timeout: int | None,
    signing_env: dict[str, str] | None = None,
    *,
    disable_hooks: bool = False,
) -> None:
    """Create a signed and DCO-signed commit."""
    hook_config = ["-c", f"core.hooksPath={os.devnull}"] if disable_hooks else []
    kwargs: dict[str, Any] = _git_timeout_kw(git_timeout)
    if signing_env is not None:
        kwargs["env"] = signing_env
    run(
        ["git", *hook_config, "commit", "-S", "-s", "-m", commit_message],
        cwd=worktree_path,
        **kwargs,
    )


def _commit_paths_from_input(
    metadata: CommitIssueMetadata,
    worktree_path: Path,
    *,
    git_timeout: int | None,
    operation_env: dict[str, str] | None,
    allowed_paths: Collection[str] | None,
    expected_add_paths: tuple[str, ...] | None,
    expected_update_paths: tuple[str, ...] | None,
) -> CommitPaths:
    """Return paths from a host manifest or bounded ordinary status."""
    manifest_supplied = expected_add_paths is not None or expected_update_paths is not None
    if manifest_supplied:
        if not isinstance(expected_add_paths, tuple) or not isinstance(
            expected_update_paths, tuple
        ):
            raise RuntimeError("The inspected commit path manifest is invalid")
        paths = CommitPaths(expected_add_paths, expected_update_paths)
        if not is_bounded_commit_paths(
            paths,
            max_paths=_COMMIT_MANIFEST_MAX_PATHS,
            max_bytes=_COMMIT_MANIFEST_MAX_BYTES,
        ):
            raise RuntimeError("The inspected commit path manifest is invalid")
        return paths
    porcelain = _read_porcelain_status(worktree_path, git_timeout, env=operation_env)
    if not porcelain:
        raise RuntimeError(
            f"No changes to commit for issue {issue_ref(metadata.number)}. "
            "Check if the implementation was successful or if the plan needs revision."
        )
    entries = parse_porcelain_status(porcelain)
    paths = select_commit_paths(entries, allowed_paths)
    if not paths.add_paths and not paths.update_paths:
        raise RuntimeError(
            f"No non-secret files to commit for issue {issue_ref(metadata.number)}. "
            "All changes appear to be secret files."
        )
    reject_filtered_path_shape_changes(entries, paths)
    return paths


def commit_changes(
    metadata: CommitIssueMetadata,
    worktree_path: Path,
    agent: str = "claude",
    git_message_timeout: int = DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    allowed_paths: Collection[str] | None = None,
    git_timeout: int | None = None,
    agent_model: str | None = None,
    *,
    expected_tree_sha: str | None = None,
    return_commit_sha: bool = False,
    signing_env: dict[str, str] | None = None,
    git_env: dict[str, str] | None = None,
    expected_add_paths: tuple[str, ...] | None = None,
    expected_update_paths: tuple[str, ...] | None = None,
    disable_hooks: bool = False,
    pi_dir: Path | None = None,
    claude_message_agent: CommitMessageAgent | None = None,
) -> str | None:
    """Stage and commit changes with closed issue metadata."""
    operation_env: dict[str, str] | None = None
    if git_env is not None or signing_env is not None:
        operation_env = dict(git_env or {})
        operation_env.update(signing_env or {})
    paths = _commit_paths_from_input(
        metadata,
        worktree_path,
        git_timeout=git_timeout,
        operation_env=operation_env,
        allowed_paths=allowed_paths,
        expected_add_paths=expected_add_paths,
        expected_update_paths=expected_update_paths,
    )
    _stage_commit_paths(paths, worktree_path, git_timeout, env=operation_env)
    if expected_tree_sha is not None:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_tree_sha) is None:
            raise RuntimeError("The expected commit tree is invalid")
        kwargs: dict[str, Any] = _git_timeout_kw(git_timeout)
        if operation_env is not None:
            kwargs["env"] = operation_env
        staged_tree = run(["git", "write-tree"], cwd=worktree_path, **kwargs).stdout.strip()
        if staged_tree != expected_tree_sha:
            raise RuntimeError("The staged commit tree changed after inspection")
    commit_message = _generate_commit_message(
        metadata,
        worktree_path,
        agent,
        git_message_timeout=git_message_timeout,
        git_timeout=git_timeout,
        agent_model=agent_model,
        pi_dir=pi_dir,
        git_env=operation_env,
        claude_message_agent=claude_message_agent,
    )
    if signing_env is None:
        _clear_local_committer_identity(worktree_path, git_timeout)
    _commit_with_signature(
        commit_message,
        worktree_path,
        git_timeout,
        operation_env,
        disable_hooks=disable_hooks,
    )
    if not return_commit_sha:
        return None
    kwargs = _git_timeout_kw(git_timeout)
    if operation_env is not None:
        kwargs["env"] = operation_env
    commit_sha = run(["git", "rev-parse", "HEAD"], cwd=worktree_path, **kwargs).stdout.strip()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_sha) is None:
        raise RuntimeError("The committed revision is unavailable")
    return commit_sha
