"""Shared git subprocess helpers for library and product layers."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from hephaestus.utils.helpers import METADATA_TIMEOUT, NETWORK_TIMEOUT, run_subprocess
from hephaestus.utils.retry import is_network_error, retry_with_backoff

logger = logging.getLogger(__name__)

_NETWORK_GIT_COMMANDS = frozenset({"clone", "fetch", "ls-remote", "pull", "push"})
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)
_GIT_GLOBAL_FLAGS = frozenset(
    {
        "--bare",
        "--glob-pathspecs",
        "--help",
        "--html-path",
        "--icase-pathspecs",
        "--info-path",
        "--literal-pathspecs",
        "--man-path",
        "--no-optional-locks",
        "--no-pager",
        "--no-replace-objects",
        "--noglob-pathspecs",
        "--paginate",
        "--version",
    }
)
_LOG_STREAM_TAIL_MAX = 2000


_REDACTED_GIT_URL = "<redacted-git-url>"
_REDACTED_VALUE = "<redacted-value>"
_GIT_URL_RE = re.compile(r"\b(?:https?|ssh|git)://\S+", re.IGNORECASE)
_GIT_SCP_REMOTE_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+@)?[\w.-]+:\S+(?:\.git)?")
_GIT_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(access_token|auth_token|oauth_token|token|password|passwd|secret|credential)="
    r"([^&\s]+)"
)
_GIT_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization:\s*(?:basic|bearer)\s+)\S+")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github"
    r"_pat_[A-Za-z0-9_]{20,})\b"
)


def _cwd_arg(cwd: Path | str | None) -> str | None:
    """Return a subprocess-compatible cwd value."""
    return str(cwd) if isinstance(cwd, Path) else cwd


def _git_command(args: list[str]) -> list[str]:
    """Normalize a git argument vector to exactly one leading ``git``."""
    normalized = list(args)
    if normalized and normalized[0] == "git":
        normalized = normalized[1:]
    return ["git", *normalized]


def _git_args(args: list[str]) -> list[str]:
    """Return git arguments without the leading executable."""
    return _git_command(args)[1:]


def _git_option_name(arg: str) -> str:
    """Return the option name without an inline ``=value`` suffix."""
    return arg.split("=", 1)[0]


def _git_subcommand(args: list[str]) -> str | None:
    """Return the git subcommand after leading global options."""
    index = 0
    while index < len(args):
        arg = args[index]
        option_name = _git_option_name(arg)
        if arg == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if arg in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if "=" in arg and option_name in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 1
            continue
        if arg in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        if arg.startswith("-"):
            return None
        return arg
    return None


def _as_text(value: object) -> str:
    """Return subprocess stream data as text for classification/logging."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _tail_for_log(value: str, limit: int = _LOG_STREAM_TAIL_MAX) -> str:
    """Return a bounded tail for Git subprocess streams."""
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"...({omitted} earlier chars){value[-limit:]}"


def _redact_git_diagnostics(value: str) -> str:
    """Return Git diagnostics with credential-bearing values redacted."""
    redacted = _GIT_AUTH_HEADER_RE.sub(r"\1" + _REDACTED_VALUE, value)
    redacted = _GIT_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}={_REDACTED_VALUE}", redacted
    )
    redacted = _GITHUB_TOKEN_RE.sub(_REDACTED_VALUE, redacted)
    redacted = _GIT_URL_RE.sub(_REDACTED_GIT_URL, redacted)
    redacted = _GIT_SCP_REMOTE_RE.sub(_REDACTED_GIT_URL, redacted)
    return redacted


def _format_git_cmd_for_log(cmd: list[str]) -> str:
    """Return a redacted Git command string for logs."""
    return " ".join(_redact_git_diagnostics(part) for part in cmd)


def _tail_for_git_log(value: str) -> str:
    """Return a bounded redacted tail for Git subprocess streams."""
    return _tail_for_log(_redact_git_diagnostics(value))


def _log_git_retry_warning(message: str) -> None:
    """Log retry utility warnings without credential-bearing Git diagnostics."""
    logger.warning(_redact_git_diagnostics(message))


def _is_retryable_git_error(error: BaseException) -> bool:
    """Return True for transient Git subprocess failures."""
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    blob = "\n".join(
        part for part in (_as_text(error.stdout), _as_text(error.stderr), str(error)) if part
    )
    return is_network_error(Exception(blob))


def _log_retry_managed_git_failure(cmd: list[str], error: BaseException) -> None:
    """Log the final failure from a retry-managed Git command."""
    logger.error("Git command failed after retry handling: %s", _format_git_cmd_for_log(cmd))
    if isinstance(error, subprocess.TimeoutExpired):
        if error.timeout is not None:
            logger.error("timeout: %s", error.timeout)
        stdout = _as_text(error.output)
        stderr = _as_text(error.stderr)
    elif isinstance(error, subprocess.CalledProcessError):
        stdout = _as_text(error.stdout)
        stderr = _as_text(error.stderr)
    else:
        logger.error("error: %s", _redact_git_diagnostics(str(error)))
        return
    if stdout:
        logger.error("stdout: %s", _tail_for_git_log(stdout))
    if stderr:
        logger.error("stderr: %s", _tail_for_git_log(stderr))


def run_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    timeout: int | None = NETWORK_TIMEOUT,
    capture_output: bool = True,
    text: bool = True,
    check: bool = True,
    dry_run: bool = False,
    log_on_error: bool = True,
    env: dict[str, str] | None = None,
    retries: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git through the repository's standard subprocess helper."""
    if not capture_output or not text:
        raise ValueError("run_git always captures text output through run_subprocess")

    normalized_args = _git_args(args)
    if retries is None:
        retries = 2 if _git_subcommand(normalized_args) in _NETWORK_GIT_COMMANDS else 0

    def _call(*, log_errors: bool = log_on_error) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {
            "cwd": _cwd_arg(cwd),
            "check": check,
            "timeout": timeout,
            "dry_run": dry_run,
            "log_on_error": log_errors,
        }
        if env is not None:
            kwargs["env"] = env
        return run_subprocess(["git", *normalized_args], **kwargs)

    if retries <= 0:
        return _call()

    @retry_with_backoff(
        max_retries=retries,
        initial_delay=1.0,
        backoff_factor=2,
        retry_on=(subprocess.CalledProcessError, subprocess.TimeoutExpired),
        logger=_log_git_retry_warning,
        jitter=True,
        retry_predicate=_is_retryable_git_error,
    )
    def _retrying_call() -> subprocess.CompletedProcess[str]:
        return _call(log_errors=False)

    try:
        return _retrying_call()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if log_on_error:
            _log_retry_managed_git_failure(["git", *normalized_args], e)
        raise


def git_config_get(key: str, *, global_: bool = False, cwd: Path | str | None = None) -> str | None:
    """Return a git config value, or None when the key is unset."""
    args = ["config"]
    if global_:
        args.append("--global")
    args.extend(["--get", key])
    try:
        result = run_git(
            args,
            cwd=cwd,
            timeout=METADATA_TIMEOUT,
            check=False,
            log_on_error=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    return value


def git_remote_url(remote: str = "origin", *, cwd: Path | str | None = None) -> str | None:
    """Return a git remote URL, or None when it cannot be read."""
    try:
        result = run_git(
            ["remote", "get-url", remote],
            cwd=cwd,
            timeout=METADATA_TIMEOUT,
            check=False,
            log_on_error=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    return value


def git_branch_exists(branch_name: str, *, cwd: Path | str | None = None) -> bool:
    """Return whether a local branch exists."""
    try:
        result = run_git(
            ["branch", "--list", branch_name],
            cwd=cwd,
            timeout=METADATA_TIMEOUT,
            check=False,
            log_on_error=False,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return bool(result.stdout.strip())


def git_push(
    cwd: Path | str | None,
    remote: str,
    refspec: str,
    *,
    dry_run: bool = False,
    retries: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Push ``refspec`` to ``remote`` from ``cwd``."""
    return run_git(
        ["push", remote, refspec],
        cwd=cwd,
        dry_run=dry_run,
        timeout=NETWORK_TIMEOUT,
        retries=retries,
    )


def git_unmerged_files(cwd: Path | str) -> list[str]:
    """Return files with unresolved merge conflicts in ``cwd``."""
    result = run_git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=cwd,
        timeout=METADATA_TIMEOUT,
    )
    return [path.strip() for path in result.stdout.splitlines() if path.strip()]


def git_rev_list_count(cwd: Path | str, revspec: str) -> int:
    """Return ``git rev-list --count`` for ``revspec`` in ``cwd``."""
    result = run_git(
        ["rev-list", "--count", revspec],
        cwd=cwd,
        timeout=METADATA_TIMEOUT,
    )
    return int(result.stdout.strip())


def _remote_ref_candidates(ref: str) -> set[str]:
    """Return exact advertised refs that satisfy ``ref``."""
    candidates = {ref}
    if ref != "HEAD" and not ref.startswith("refs/"):
        candidates.add(f"refs/heads/{ref}")
    return candidates


def git_ls_remote_contains(
    cwd: Path | str,
    remote: str,
    ref: str,
    *,
    raise_on_error: bool = False,
) -> bool:
    """Return whether ``remote`` advertises ``ref`` as an exact ref."""
    try:
        result = run_git(
            ["ls-remote", remote, ref],
            cwd=cwd,
            timeout=NETWORK_TIMEOUT,
            check=True,
            log_on_error=False,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        if raise_on_error:
            raise
        return False

    candidates = _remote_ref_candidates(ref)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] in candidates:
            return True
    return False


def working_tree_clean() -> bool:
    """Return True if the current git working tree has no uncommitted changes."""
    result = run_git(
        ["status", "--porcelain"],
        timeout=METADATA_TIMEOUT,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def in_git_repo() -> bool:
    """Return True if the current directory is inside a git repository."""
    result = run_git(
        ["rev-parse", "--git-dir"],
        timeout=METADATA_TIMEOUT,
        check=False,
    )
    return result.returncode == 0


def repo_root() -> Path:
    """Return the root directory of the current git repository."""
    result = run_git(
        ["rev-parse", "--show-toplevel"],
        timeout=METADATA_TIMEOUT,
    )
    return Path(result.stdout.strip())


__all__ = [
    "git_branch_exists",
    "git_config_get",
    "git_ls_remote_contains",
    "git_push",
    "git_remote_url",
    "git_rev_list_count",
    "git_unmerged_files",
    "in_git_repo",
    "repo_root",
    "run_git",
    "working_tree_clean",
]
