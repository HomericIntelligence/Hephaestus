"""Pull-request-bound worktree preparation and publication for workflow skills.

Each repair binds to one observed pull request. ``hephaestus-prepare-pr-worktree``
creates a receipt-owned bare Git namespace, worktree, and immutable
preparation receipt binding the repository, pull request number, base, head,
start point, and creation mode. ``hephaestus-publish-pr-worktree`` revalidates
every live pull request field, rejects unrelated ancestry, and publishes with
an exact expected-head lease. One independent pull request gets one
integration root; only an explicit ``stacked`` receipt admits dependency
ancestry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from hephaestus.cli.localization import text
from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
)
from hephaestus.config.child_environments import build_git_child_env, build_remote_git_env
from hephaestus.errors import boundary_main
from hephaestus.git.config_safety import unsafe_local_git_config_key
from hephaestus.git.gh.client import gh_call, positive_timeout
from hephaestus.git.runtime import operation_file_lock
from hephaestus.github.remote_git import (
    add_gh_extra_path_root_arg,
    trusted_gh_authenticated,
    trusted_gh_executable,
    trusted_remote_git_config,
)
from hephaestus.github.skill_pr_review import repository_from_pr_url, validate_pr_identifier
from hephaestus.github.skill_worktrees import reject_symlinks_below
from hephaestus.utils.git import GitPreLaunchError, run_git

logger = logging.getLogger(__name__)

#: The receipt schema owned by this module. Field sets are exact.
RECEIPT_SCHEMA_VERSION = 1
#: Open pull requests are read in explicit pages of this size.
CENSUS_PAGE_SIZE = 100
#: At most this many census pages prove a complete traversal.
CENSUS_MAX_PAGES = 100

MODE_IN_PLACE = "in-place"
MODE_CLEAN_REBUILD = "clean-rebuild"
MODE_STACKED = "stacked"
_CREATION_MODES = (MODE_IN_PLACE, MODE_CLEAN_REBUILD, MODE_STACKED)

_EXIT_PUBLISHED = 0
_EXIT_REJECTED = 1
_EXIT_ARGUMENT_ERROR = 2
_EXIT_TEMPORARY = 75
_EXIT_UNKNOWN = 76
_EXIT_INTERRUPTED = 130

_DEFAULT_GH_TIMEOUT = 120

_COMMIT_OID = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+\Z")
_ORIGIN_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+\Z")
_RECEIPT_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_CENSUS_REF_PREFIX = "refs/pr-worktree-census/"
_START_REF_PREFIX = "refs/pr-worktree-start/"
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
# The namespace's own pinned safety overrides, admitted by exact key and value.
_PINNED_NAMESPACE_CONFIG = frozenset(
    {
        ("core.hookspath", os.devnull),
        ("core.fsmonitor", "false"),
    }
)

_FLAT_PR_FIELDS = frozenset(
    {
        "base_ref",
        "base_repository",
        "base_sha",
        "head_ref",
        "head_repository",
        "head_sha",
        "number",
        "state",
        "url",
    }
)
_REST_PR_FIELDS = frozenset({"base", "head", "html_url", "number", "state"})
_RECEIPT_FIELDS = frozenset(
    {
        "base",
        "census",
        "dependency",
        "expected_remote_head",
        "head",
        "mode",
        "namespace",
        "receipt_digest",
        "receipt_id",
        "schema_version",
        "source",
        "start",
        "state",
        "target",
        "worktree",
    }
)
_NAMESPACE_FIELDS = frozenset(
    {"config_digest", "directory_identity", "manifest_digest", "path", "permitted_refs"}
)
_MANIFEST_FIELDS = frozenset(
    {"digest", "config_digest", "directory_identity", "namespace", "permitted_refs"}
)
_WORKTREE_FIELDS = frozenset({"gitdir", "gitdir_identity", "local_branch", "path"})
_OUTCOME_FIELDS = frozenset(
    {"candidate_object", "message", "receipt_id", "schema_version", "state"}
)


# ---------------------------------------------------------------------------
# Classified failures
# ---------------------------------------------------------------------------


class ContractRejectedError(RuntimeError):
    """A contract validation failed; publication must not continue."""


class TemporaryFailureError(RuntimeError):
    """GitHub or Git failed before a possible write; a bounded retry may recover."""


def _rejected(message: str) -> NoReturn:
    """Raise the rejected failure with one concise reason."""
    raise ContractRejectedError(message)


def _temporary(message: str) -> NoReturn:
    """Raise the temporary failure with one concise reason."""
    raise TemporaryFailureError(message)


# ---------------------------------------------------------------------------
# Pull-request model and strict parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequestView:
    """One validated open pull request with separate base and head identities."""

    number: int
    url: str
    state: str
    base_repository: str
    base_ref: str
    base_sha: str
    head_repository: str
    head_ref: str
    head_sha: str

    def to_flat(self) -> dict[str, Any]:
        """Return the strict flat mapping stored in receipts and censuses."""
        return {
            "base_ref": self.base_ref,
            "base_repository": self.base_repository,
            "base_sha": self.base_sha,
            "head_ref": self.head_ref,
            "head_repository": self.head_repository,
            "head_sha": self.head_sha,
            "number": self.number,
            "state": self.state,
            "url": self.url,
        }

    @classmethod
    def from_flat(cls, payload: object) -> PullRequestView:
        """Rebuild one validated view from its strict flat mapping."""
        shape = _require_shape(payload, _FLAT_PR_FIELDS, "pull-request row")
        view = cls(
            number=shape["number"],
            url=shape["url"],
            state=shape["state"],
            base_repository=shape["base_repository"],
            base_ref=shape["base_ref"],
            base_sha=shape["base_sha"],
            head_repository=shape["head_repository"],
            head_ref=shape["head_ref"],
            head_sha=shape["head_sha"],
        )
        _validate_view_fields(view)
        return view


def _require_shape(payload: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    """Require a mapping with exactly the expected keys."""
    if not isinstance(payload, dict):
        _rejected(f"{label} must be a JSON object")
    extra = sorted(set(payload) - fields)
    missing = sorted(fields - set(payload))
    if extra or missing:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        _rejected(f"{label} has an invalid field set: {'; '.join(details)}")
    return payload


def invalid_branch_reason(ref: object) -> str | None:
    """Return why *ref* is not a valid Git branch name, or None when valid.

    The rules mirror ``git check-ref-format`` so pull-request validation stays
    hermetic; the real command still validates the caller-supplied branch.
    """
    if not isinstance(ref, str) or not ref:
        return "it is empty"
    if ref.startswith("-"):
        return "it starts with '-'"
    if ref == "@":
        return "it is the single '@'"
    return _branch_character_reason(ref) or _branch_component_reason(ref)


def _branch_character_reason(ref: str) -> str | None:
    """Return the whole-name branch rule that *ref* breaks, if any."""
    if any(character in ref for character in "~^:?*[\\"):
        return "it holds a forbidden character"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in ref):
        return "it holds a control character"
    if " " in ref:
        return "it holds a space"
    if ".." in ref:
        return "it holds '..'"
    if "@{" in ref:
        return "it holds '@{'"
    return None


def _branch_component_reason(ref: str) -> str | None:
    """Return the path-component branch rule that *ref* breaks, if any."""
    if ref.startswith("/") or ref.endswith("/") or "//" in ref:
        return "it has an empty path component"
    if ref.endswith("."):
        return "it ends with '.'"
    for component in ref.split("/"):
        if component.startswith("."):
            return "it holds a component starting with '.'"
        if component.endswith(".lock"):
            return "it holds a component ending with '.lock'"
    return None


def _require_branch(value: object, label: str) -> str:
    reason = invalid_branch_reason(value)
    if reason is not None:
        _rejected(f"{label} is not a valid Git branch name: {reason}")
    return str(value)


def _require_oid(value: object, label: str) -> str:
    if not isinstance(value, str) or _COMMIT_OID.fullmatch(value) is None:
        _rejected(f"{label} must be a lowercase 40-hex Git object identifier")
    return value


def _require_repository(value: object, label: str) -> str:
    if not isinstance(value, str) or _GITHUB_REPOSITORY.fullmatch(value) is None:
        _rejected(f"{label} must be a canonical GitHub owner/repository")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _rejected(f"{label} must be a positive integer")
    return int(value)


def _require_identity_list(value: object, label: str) -> None:
    """Require one host directory identity of exactly two integers."""
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        _rejected(f"{label} must be a two-integer directory identity")


def _validate_view_fields(view: PullRequestView) -> None:
    """Validate every identity field of one pull request view."""
    _require_repository(view.base_repository, "pull-request base repository")
    _require_repository(view.head_repository, "pull-request head repository")
    _require_branch(view.base_ref, "pull-request base branch")
    _require_branch(view.head_ref, "pull-request head branch")
    _require_oid(view.base_sha, "pull-request base tip")
    _require_oid(view.head_sha, "pull-request head tip")
    try:
        repository = repository_from_pr_url(view.url, view.number)
    except RuntimeError as error:
        _rejected(str(error))
    if repository.casefold() != view.base_repository.casefold():
        _rejected("pull-request URL does not identify its base repository")


def pull_request_from_rest(payload: object, expected_repository: str) -> PullRequestView:
    """Parse and validate one REST pull-request object from its base repository."""
    shape = _require_shape(payload, _REST_PR_FIELDS, "GitHub pull request")
    number = shape["number"]
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        _rejected("GitHub pull request number must be a positive integer")
    if shape["state"] != "OPEN":
        _rejected(f"pull request {number} is not open")
    url = shape["html_url"]
    if not isinstance(url, str):
        _rejected("GitHub pull request html_url must be a string")
    base = _rest_endpoint(shape["base"], "base")
    head = _rest_endpoint(shape["head"], "head")
    view = PullRequestView(
        number=number,
        url=url,
        state="OPEN",
        base_repository=base[0],
        base_ref=base[1],
        base_sha=base[2],
        head_repository=head[0],
        head_ref=head[1],
        head_sha=head[2],
    )
    _validate_view_fields(view)
    if view.base_repository.casefold() != expected_repository.casefold():
        _rejected(f"pull request {view.url} does not belong to repository {expected_repository}")
    return view


def _rest_endpoint(endpoint: object, label: str) -> tuple[str, str, str]:
    """Validate one REST base or head endpoint; return repository, ref, and sha."""
    shape = _require_shape(endpoint, frozenset({"ref", "repo", "sha"}), f"pull-request {label}")
    repository = shape["repo"]
    if not isinstance(repository, dict):
        _rejected(f"pull-request {label} repository must be a JSON object")
    full_name = repository.get("full_name")
    if not isinstance(full_name, str):
        _rejected(f"pull-request {label} repository full_name must be a string")
    return (
        _require_repository(full_name, f"pull-request {label} repository"),
        _require_branch(shape["ref"], f"pull-request {label} branch"),
        _require_oid(shape["sha"], f"pull-request {label} tip"),
    )


def canonical_repository_url(repository: str) -> str:
    """Return the canonical https clone URL for one owner/repository."""
    return f"https://github.com/{_require_repository(repository, 'repository')}.git"


def pull_request_number(identifier: str) -> int:
    """Return the positive pull-request number behind a number or canonical URL."""
    validate_pr_identifier(identifier)
    if identifier.isdigit():
        return int(identifier)
    return int(identifier.rstrip("/").rsplit("/", maxsplit=1)[-1])


# ---------------------------------------------------------------------------
# Controlled GitHub and remote Git boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteSession:
    """One authenticated remote Git and GitHub read session."""

    gh_command: str
    timeout_s: int


def remote_session(extra_path_root: str | None, timeout_s: int) -> RemoteSession:
    """Resolve the trusted gh executable and require working authentication.

    Args:
        extra_path_root: Validated ``--gh-extra-path-root`` value, or None.
        timeout_s: Bounded timeout for the authentication probe.

    Returns:
        The authenticated session with its trusted gh command.

    Raises:
        TemporaryFailure: When authentication is unavailable.

    """
    command = trusted_gh_executable(Path(extra_path_root)) if extra_path_root else None
    if command is None:
        command = "gh"
    if not trusted_gh_authenticated(command, timeout_s):
        _temporary("remote Git authentication unavailable")
    return RemoteSession(gh_command=command, timeout_s=timeout_s)


def _resolved_executable(extra_path_root: str | None) -> str:
    """Return the trusted gh executable argument for GitHub reads."""
    if not extra_path_root:
        return "gh"
    command = trusted_gh_executable(Path(extra_path_root))
    return command if command is not None else "gh"


def _gh_api_read(arguments: list[str], *, timeout_s: int, executable: str) -> str:
    """Read one bounded GitHub REST payload through the shared adapter."""
    result = gh_call(
        arguments,
        check=False,
        timeout=timeout_s,
        executable=executable,
        retry_on_rate_limit=True,
        throttle=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "HTTP 404" in stderr or "Not Found" in stderr:
            _rejected(f"GitHub request failed: {stderr}")
        _temporary(f"GitHub request failed: {stderr}")
    return result.stdout


def _read_pull_request(
    session: RemoteSession, repository: str, number: int, *, executable: str
) -> PullRequestView:
    """Read and validate one live pull request from its base repository."""
    stdout = _gh_api_read(
        ["api", f"repos/{repository}/pulls/{number}"],
        timeout_s=session.timeout_s,
        executable=executable,
    )
    try:
        payload: Any = json.loads(stdout)
    except json.JSONDecodeError as error:
        _rejected(f"GitHub returned an invalid pull-request payload: {error}")
    return pull_request_from_rest(payload, repository)


def read_open_pull_request_census(
    session: RemoteSession, base_repository: str, *, executable: str
) -> dict[int, PullRequestView]:
    """Read every open pull request in bounded pages from the base repository.

    The census requests explicit pages of 100 open pull requests and permits
    at most 100 pages. A final page that is still full prevents proof of a
    complete traversal and is rejected.

    Args:
        session: Authenticated remote session.
        base_repository: Owner/repository that owns the pull requests.
        executable: Trusted ``gh`` executable argument.

    Returns:
        Every validated open pull request keyed by its number.

    Raises:
        ContractRejected: When the census is malformed or cannot prove a
            complete traversal.
        TemporaryFailure: When a GitHub read fails transiently.

    """
    rows: dict[int, PullRequestView] = {}
    page = 1
    while True:
        if page > CENSUS_MAX_PAGES:
            _rejected(
                f"open pull-request census did not complete within {CENSUS_MAX_PAGES} "
                f"pages of {CENSUS_PAGE_SIZE}; complete traversal cannot be proven"
            )
        stdout = _gh_api_read(
            ["api", f"repos/{base_repository}/pulls?state=open&per_page=100&page={page}"],
            timeout_s=session.timeout_s,
            executable=executable,
        )
        items = _census_page_rows(stdout, base_repository, page)
        # Duplicates are a per-page defect: GitHub pagination never repeats a
        # row on a later page, and a repeated number cannot add a new sibling.
        numbers = [view.number for view in items]
        if len(set(numbers)) != len(numbers):
            _rejected(f"census page {page} returned duplicate pull requests")
        rows.update({view.number: view for view in items})
        if len(items) < CENSUS_PAGE_SIZE:
            return rows
        page += 1


def _census_page_rows(stdout: str, base_repository: str, page: int) -> list[PullRequestView]:
    """Validate one census page and return its pull-request views."""
    try:
        payload: Any = json.loads(stdout)
    except json.JSONDecodeError as error:
        _rejected(f"GitHub returned an invalid census page {page}: {error}")
    if not isinstance(payload, list):
        _rejected(f"GitHub census page {page} must be a JSON array")
    return [pull_request_from_rest(item, base_repository) for item in payload]


def _remote_git_run(
    session: RemoteSession,
    cwd: Path,
    arguments: list[str],
    *,
    pre_launch: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one remote Git command with the trusted transport configuration."""
    config = trusted_remote_git_config(session.gh_command)
    if config is None:
        _temporary("remote Git transport configuration unavailable")
    return run_git(
        [*config, *arguments],
        cwd=cwd,
        env=build_remote_git_env(),
        timeout=session.timeout_s,
        check=False,
        log_on_error=False,
        retries=0,
        pre_launch=pre_launch,
    )


# ---------------------------------------------------------------------------
# Local Git helpers
# ---------------------------------------------------------------------------


def _git_quiet(cwd: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded local Git command in the hermetic child environment."""
    return run_git(arguments, cwd=cwd, check=False, log_on_error=False, env=build_git_child_env())


def _git_output(cwd: Path, arguments: list[str]) -> str:
    """Run one local Git command and return stdout, or raise a rejected failure."""
    result = _git_quiet(cwd, arguments)
    if result.returncode != 0:
        _rejected(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout


def _directory_identity(path: Path) -> tuple[int, int]:
    """Return the host directory identity of *path*, refusing anything else."""
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        _rejected(f"directory identity unavailable for {path}: {error}")
    if not stat.S_ISDIR(metadata.st_mode):
        _rejected(f"expected a directory at {path}")
    return metadata.st_dev, metadata.st_ino


def _canonical_digest(payload: object) -> str:
    """Return the SHA-256 digest of the deterministic JSON encoding."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _git_config_entries(config_file: Path) -> list[tuple[str, str]]:
    """Read one Git configuration file with explicit no-include arguments."""
    if not config_file.is_file():
        return []
    result = _git_quiet(
        config_file.parent,
        ["config", f"--file={config_file}", "--no-includes", "--null", "--list"],
    )
    if result.returncode != 0:
        _rejected(f"cannot read Git configuration file {config_file}")
    entries: list[tuple[str, str]] = []
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        entries.append((key, value))
    return entries


def _unsafe_config_entry(
    entries: Sequence[tuple[str, str]],
    *,
    pinned: frozenset[tuple[str, str]] = frozenset(),
) -> str | None:
    """Return the first unsafe configuration entry, ignoring pinned overrides."""
    for key, value in entries:
        if unsafe_local_git_config_key(f"{key}\n{value}") is None:
            continue
        if (key.casefold(), value) in pinned:
            continue
        return key
    return None


def _config_digest(entries: Sequence[tuple[str, str]]) -> str:
    """Return the digest of one configuration file's sorted entries."""
    return _canonical_digest(sorted(entries))


def _is_ancestor(namespace: Path, ancestor: str, descendant: str) -> bool:
    """Return whether one namespace object is an ancestor of another."""
    result = _git_quiet(namespace, ["merge-base", "--is-ancestor", ancestor, descendant])
    if result.returncode in (0, 1):
        return result.returncode == 0
    _rejected(result.stderr.strip() or "merge-base ancestry check failed")


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def _validated_repository_root(value: str) -> Path:
    """Validate the source checkout used only as the read-only source."""
    root = Path(value).expanduser()
    if not root.is_absolute():
        _rejected("--repository-root must be an absolute path")
    if not root.is_dir():
        _rejected(f"--repository-root is not a directory: {root}")
    reject_symlinks_below(root.parent, root)
    inside = _git_quiet(root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        _rejected(f"--repository-root is not a Git working tree: {root}")
    return root


def _validated_build_subdirectory(repository_root: Path, value: str, option: str) -> Path:
    """Validate one absolute directory below ``<repository-root>/build/``."""
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        _rejected(f"{option} must be an absolute path")
    build_root = repository_root / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    reject_symlinks_below(build_root, directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _validated_new_worktree_path(worktree_root: Path, value: str) -> Path:
    """Validate a new absolute worktree path below the worktree root."""
    worktree = Path(value).expanduser()
    if not worktree.is_absolute():
        _rejected("--worktree must be an absolute path")
    reject_symlinks_below(worktree_root, worktree)
    if worktree.exists():
        _rejected(f"worktree path already exists: {worktree}")
    worktree.mkdir(parents=True)
    return worktree


def _read_source_configuration(repository_root: Path, target_repository: str) -> tuple[str, str]:
    """Bind the source checkout's origin and configuration digest to the target.

    Returns:
        The normalized origin fetch URL and the configuration digest.

    Raises:
        ContractRejected: When the configuration is unsafe or the origin does
            not identify the target base repository.

    """
    git_dir = Path(_git_output(repository_root, ["rev-parse", "--absolute-git-dir"]).strip())
    common_dir = Path(_git_output(repository_root, ["rev-parse", "--git-common-dir"]).strip())
    common_dir = common_dir if common_dir.is_absolute() else repository_root / common_dir
    entries = _git_config_entries(common_dir / "config")
    entries.extend(_git_config_entries(common_dir / "config.worktree"))
    entries.extend(_git_config_entries(git_dir / "config.worktree"))
    unsafe = _unsafe_config_entry(entries)
    if unsafe is not None:
        _rejected(f"repository configuration holds unsafe key: {unsafe}")
    origin_values = [value for key, value in entries if key.casefold() == "remote.origin.url"]
    if len(origin_values) != 1:
        _rejected("repository configuration must hold exactly one origin fetch URL")
    return _normalized_origin(origin_values[0], target_repository), _config_digest(entries)


def _normalized_origin(origin: str, target_repository: str) -> str:
    """Require one https origin that identifies the target base repository."""
    candidate = origin.strip()
    if _ORIGIN_URL.fullmatch(candidate) is None:
        _rejected(f"origin is not a canonical https GitHub URL: {origin}")
    repository = (
        candidate.removeprefix("https://github.com/").removesuffix("/").removesuffix(".git")
    )
    if repository.casefold() != target_repository.casefold():
        _rejected(
            f"origin {repository} does not identify the target repository {target_repository}"
        )
    return f"https://github.com/{repository}.git"


# ---------------------------------------------------------------------------
# Protected no-follow file operations
# ---------------------------------------------------------------------------


def _write_protected_file(directory: Path, name: str, content: str) -> None:
    """Atomically write a mode-0600 file without following a replaced name."""
    directory_fd = os.open(directory, _DIR_OPEN_FLAGS)
    try:
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, _WRITE_OPEN_FLAGS, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise
    finally:
        os.close(directory_fd)


def _read_protected_file(directory: Path, name: str) -> Any:
    """Read one regular no-follow file from a validated directory."""
    directory_fd = os.open(directory, _DIR_OPEN_FLAGS)
    try:
        fd = os.open(name, _FILE_OPEN_FLAGS, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                _rejected(f"{directory / name} is not a regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                try:
                    return json.load(handle)
                except json.JSONDecodeError as error:
                    _rejected(f"{directory / name} is not valid JSON: {error}")
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(directory_fd)


# ---------------------------------------------------------------------------
# Namespace, manifest, and receipt bindings
# ---------------------------------------------------------------------------


def _create_namespace(state_root: Path, receipt_id: str) -> Path:
    """Create the receipt-owned bare Git namespace below the state root."""
    namespace = state_root / "git-namespace" / receipt_id
    reject_symlinks_below(state_root, namespace)
    namespace.parent.mkdir(parents=True, exist_ok=True)
    result = _git_quiet(state_root, ["init", "--bare", "--quiet", str(namespace)])
    if result.returncode != 0:
        _rejected(f"cannot create the Git namespace: {result.stderr.strip()}")
    for key, value in (("core.hooksPath", os.devnull), ("core.fsmonitor", "false")):
        result = _git_quiet(namespace, ["config", key, value])
        if result.returncode != 0:
            _rejected(f"cannot configure the Git namespace: {result.stderr.strip()}")
    return namespace


def _namespace_manifest_payload(namespace: Path, permitted_refs: tuple[str, ...]) -> dict[str, Any]:
    """Return the strict namespace manifest payload after configuration checks."""
    identity = _directory_identity(namespace)
    config_entries = _git_config_entries(namespace / "config")
    unsafe = _unsafe_config_entry(config_entries, pinned=_PINNED_NAMESPACE_CONFIG)
    if unsafe is not None:
        _rejected(f"namespace configuration holds unsafe key: {unsafe}")
    return {
        "config_digest": _config_digest(config_entries),
        "directory_identity": [identity[0], identity[1]],
        "namespace": str(namespace),
        "permitted_refs": sorted(permitted_refs),
    }


def _write_namespace_manifest(
    namespace: Path, permitted_refs: tuple[str, ...]
) -> tuple[tuple[int, int], str, str]:
    """Write the namespace manifest and return identity, manifest, and config digests."""
    payload = _namespace_manifest_payload(namespace, permitted_refs)
    digest = _canonical_digest(payload)
    document = {"digest": digest, **payload}
    _write_protected_file(
        namespace,
        "hephaestus-namespace-manifest.json",
        json.dumps(document, indent=2, sort_keys=True),
    )
    identity = (payload["directory_identity"][0], payload["directory_identity"][1])
    return identity, digest, payload["config_digest"]


def _load_namespace_state(receipt: dict[str, Any]) -> None:
    """Revalidate the namespace against its receipt and manifest bindings.

    Raises:
        ContractRejected: When the namespace, manifest, configuration, refs,
            or object store no longer match the receipt.

    """
    binding = receipt["namespace"]
    namespace = Path(binding["path"])
    if not namespace.is_dir():
        _rejected(f"namespace directory is missing: {namespace}")
    if _directory_identity(namespace) != tuple(binding["directory_identity"]):
        _rejected("namespace directory identity changed since preparation")
    try:
        manifest = _read_protected_file(namespace, "hephaestus-namespace-manifest.json")
    except OSError as error:
        _rejected(f"cannot read the namespace manifest: {error}")
    _require_shape(manifest, _MANIFEST_FIELDS, "namespace manifest")
    payload = {key: value for key, value in manifest.items() if key != "digest"}
    if _canonical_digest(payload) != manifest["digest"]:
        _rejected("namespace manifest digest does not match its content")
    if manifest["digest"] != binding["manifest_digest"]:
        _rejected("namespace manifest changed since preparation")
    if manifest["config_digest"] != binding["config_digest"]:
        _rejected("namespace configuration changed since preparation")
    config_entries = _git_config_entries(namespace / "config")
    unsafe = _unsafe_config_entry(config_entries, pinned=_PINNED_NAMESPACE_CONFIG)
    if unsafe is not None:
        _rejected(f"namespace configuration holds unsafe key: {unsafe}")
    if _config_digest(config_entries) != manifest["config_digest"]:
        _rejected("namespace configuration digest changed since preparation")
    if not (namespace / "objects").is_dir():
        _rejected("namespace object store is missing")
    _validate_namespace_refs(namespace, manifest["permitted_refs"])


def _validate_namespace_refs(namespace: Path, permitted: object) -> None:
    """Require every namespace ref to match one permitted exact ref or prefix."""
    if (
        not isinstance(permitted, list)
        or not permitted
        or not all(isinstance(entry, str) for entry in permitted)
    ):
        _rejected("namespace manifest must list permitted refs")
    output = _git_output(namespace, ["for-each-ref", "--format=%(refname)"])
    for existing in output.splitlines():
        if not any(
            existing == entry or (entry.endswith("*") and existing.startswith(entry[:-1]))
            for entry in permitted
        ):
            _rejected(f"namespace holds an unpermitted ref: {existing}")


# ---------------------------------------------------------------------------
# Preparation receipt
# ---------------------------------------------------------------------------


def _build_preparation_receipt(
    *,
    receipt_id: str,
    target: dict[str, Any],
    view: PullRequestView,
    mode: str,
    start: dict[str, str],
    dependency: PullRequestView | None,
    census: dict[int, PullRequestView],
    source: dict[str, str],
    namespace: dict[str, Any],
    worktree: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Build the immutable preparation receipt and return it with its digest."""
    immutable: dict[str, Any] = {
        "base": {
            "ref": view.base_ref,
            "repository": view.base_repository,
            "sha": view.base_sha,
        },
        "census": {str(number): row.to_flat() for number, row in sorted(census.items())},
        "dependency": dependency.to_flat() if dependency is not None else None,
        "expected_remote_head": view.head_sha,
        "head": {
            "ref": view.head_ref,
            "repository": view.head_repository,
            "sha": view.head_sha,
        },
        "mode": mode,
        "namespace": namespace,
        "receipt_id": receipt_id,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source": source,
        "start": start,
        "state": "prepared",
        "target": target,
        "worktree": worktree,
    }
    digest = _canonical_digest(immutable)
    return {"receipt_digest": digest, **immutable}, digest


def _validate_receipt_shape(receipt: object) -> dict[str, Any]:
    """Require the exact strict schema-version-1 receipt shape."""
    receipt = _require_shape(receipt, _RECEIPT_FIELDS, "preparation receipt")
    _validate_receipt_core(receipt)
    _validate_receipt_target(receipt["target"])
    _validate_receipt_endpoints(receipt)
    _validate_receipt_start(receipt["start"])
    _validate_receipt_source(receipt["source"])
    _validate_receipt_census(receipt["census"])
    _validate_receipt_bindings(receipt)
    _receipt_digest_matches(receipt)
    return receipt


def _validate_receipt_core(receipt: dict[str, Any]) -> None:
    """Require the receipt identity, state, mode, and digest fields."""
    if isinstance(receipt["schema_version"], bool) or (
        receipt["schema_version"] != RECEIPT_SCHEMA_VERSION
    ):
        _rejected("preparation receipt schema version is not supported")
    if receipt["state"] != "prepared":
        _rejected("preparation receipt state is not the immutable prepared state")
    if receipt["mode"] not in _CREATION_MODES:
        _rejected("preparation receipt mode is not a creation mode")
    receipt_id = receipt["receipt_id"]
    if not isinstance(receipt_id, str) or not receipt_id:
        _rejected("preparation receipt identifier must be a non-empty string")
    if (
        not isinstance(receipt["receipt_digest"], str)
        or _SHA256.fullmatch(receipt["receipt_digest"]) is None
    ):
        _rejected("preparation receipt digest must be a 64-hex digest")


def _validate_receipt_target(target: object) -> None:
    """Require the strict receipt target binding."""
    target = _require_shape(
        target, frozenset({"host", "number", "repository", "url"}), "receipt target"
    )
    if target["host"] != "github.com":
        _rejected("receipt target host must be github.com")
    _require_positive_int(target["number"], "receipt target number")
    _require_repository(target["repository"], "receipt target repository")


def _validate_receipt_endpoints(receipt: dict[str, Any]) -> None:
    """Require the strict receipt base, head, and dependency bindings."""
    for label in ("base", "head"):
        endpoint = _require_shape(
            receipt[label], frozenset({"ref", "repository", "sha"}), f"receipt {label}"
        )
        _require_repository(endpoint["repository"], f"receipt {label} repository")
        _require_branch(endpoint["ref"], f"receipt {label} branch")
        _require_oid(endpoint["sha"], f"receipt {label} tip")
    dependency = receipt["dependency"]
    if dependency is not None:
        PullRequestView.from_flat(dependency)


def _validate_receipt_start(start: object) -> None:
    """Require the strict receipt start binding."""
    start = _require_shape(start, frozenset({"object", "ref", "repository"}), "receipt start")
    _require_repository(start["repository"], "receipt start repository")
    _require_branch(start["ref"], "receipt start branch")
    _require_oid(start["object"], "receipt start object")


def _validate_receipt_source(source: object) -> None:
    """Require the strict receipt source binding."""
    source = _require_shape(
        source,
        frozenset({"config_digest", "origin_fetch_url", "repository_root"}),
        "receipt source",
    )
    for field in ("config_digest", "origin_fetch_url", "repository_root"):
        if not isinstance(source[field], str) or not source[field]:
            _rejected(f"receipt source {field} must be a non-empty string")


def _validate_receipt_census(census: object) -> None:
    """Require the strict receipt census rows."""
    if not isinstance(census, dict):
        _rejected("receipt census must be an object")
    for key, row in census.items():
        if not key.isdigit():
            _rejected("receipt census keys must be pull-request numbers")
        PullRequestView.from_flat(row)


def _validate_receipt_bindings(receipt: dict[str, Any]) -> None:
    """Require the strict receipt namespace and worktree bindings."""
    namespace = _require_shape(receipt["namespace"], _NAMESPACE_FIELDS, "receipt namespace")
    _require_identity_list(namespace["directory_identity"], "receipt namespace identity")
    if (
        not isinstance(namespace["manifest_digest"], str)
        or _SHA256.fullmatch(namespace["manifest_digest"]) is None
    ):
        _rejected("receipt namespace manifest digest must be a 64-hex digest")
    if not isinstance(namespace["permitted_refs"], list) or not all(
        isinstance(entry, str) for entry in namespace["permitted_refs"]
    ):
        _rejected("receipt namespace permitted refs must be strings")
    worktree = _require_shape(receipt["worktree"], _WORKTREE_FIELDS, "receipt worktree")
    if invalid_branch_reason(worktree["local_branch"]) is not None:
        _rejected("receipt local branch is not a valid Git branch name")
    _require_identity_list(worktree["gitdir_identity"], "receipt worktree identity")
    for field in ("gitdir", "path"):
        if not isinstance(worktree[field], str) or not worktree[field]:
            _rejected(f"receipt worktree {field} must be a non-empty string")
    _require_oid(receipt["expected_remote_head"], "receipt expected remote head")


def _receipt_digest_matches(receipt: dict[str, Any]) -> None:
    """Recompute the digest of the immutable receipt fields."""
    immutable = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if _canonical_digest(immutable) != receipt["receipt_digest"]:
        _rejected("preparation receipt digest does not match its immutable fields")


def _load_preparation_receipt(receipt_path: Path, state_root: Path) -> dict[str, Any]:
    """Load, validate, and digest-check the preparation receipt below the state root."""
    absolute_receipt = receipt_path.expanduser()
    if not absolute_receipt.is_absolute():
        _rejected("--receipt must be an absolute path")
    reject_symlinks_below(state_root, absolute_receipt)
    if absolute_receipt.is_dir():
        _rejected(f"--receipt is a directory: {absolute_receipt}")
    if _RECEIPT_NAME.fullmatch(absolute_receipt.name) is None:
        _rejected("--receipt must be a digest-named preparation receipt file")
    try:
        payload = _read_protected_file(absolute_receipt.parent, absolute_receipt.name)
    except OSError as error:
        _rejected(f"cannot read the preparation receipt: {error}")
    receipt = _validate_receipt_shape(payload)
    if Path(receipt["namespace"]["path"]).parent.parent != state_root:
        _rejected("preparation receipt namespace does not live below the exact state root")
    return receipt


def _bind_target(arguments: argparse.Namespace, receipt: dict[str, Any]) -> None:
    """Bind the explicit publish target and identifier to the receipt."""
    if arguments.target_host != "github.com":
        _rejected("--target-host must be github.com")
    target = receipt["target"]
    if arguments.target_repository.casefold() != target["repository"].casefold():
        _rejected("--target-repository does not match the preparation receipt")
    identifier = arguments.pull_request
    number = pull_request_number(identifier)
    if number != target["number"]:
        _rejected("pull request identifier does not match the preparation receipt")
    if identifier.startswith("https://"):
        try:
            repository = repository_from_pr_url(identifier, number)
        except RuntimeError as error:
            _rejected(str(error))
        if repository.casefold() != target["repository"].casefold():
            _rejected("pull request URL does not match the preparation receipt")


# ---------------------------------------------------------------------------
# Outcome records and receipt lock
# ---------------------------------------------------------------------------


def _write_outcome_record(
    state_root: Path, receipt: dict[str, Any], record: dict[str, Any]
) -> None:
    """Write one locked outcome record for the receipt."""
    document = {"schema_version": RECEIPT_SCHEMA_VERSION, **record}
    _require_shape(document, _OUTCOME_FIELDS, "outcome record")
    outcomes = state_root / "outcomes"
    outcomes.mkdir(parents=True, exist_ok=True)
    _write_protected_file(
        outcomes, f"{receipt['receipt_id']}.json", json.dumps(document, indent=2, sort_keys=True)
    )


def _read_outcome_record(state_root: Path, receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Read one outcome record, or None when publication has not started."""
    outcomes = state_root / "outcomes"
    name = f"{receipt['receipt_id']}.json"
    if not (outcomes / name).is_file():
        return None
    try:
        record = _read_protected_file(outcomes, name)
    except OSError as error:
        _rejected(f"cannot read the outcome record: {error}")
    return _require_shape(record, _OUTCOME_FIELDS, "outcome record")


def _record_outcome(
    state_root: Path,
    receipt: dict[str, Any],
    *,
    state: str,
    candidate_object: str | None,
    message: str,
) -> None:
    """Record one immutable outcome state for later reconciliation."""
    _write_outcome_record(
        state_root,
        receipt,
        {
            "candidate_object": candidate_object,
            "message": message,
            "receipt_id": receipt["receipt_id"],
            "state": state,
        },
    )


@contextmanager
def _hold_receipt_lock(state_root: Path, receipt_id: str) -> Iterator[None]:
    """Hold the receipt lock across publication preflight and outcome updates.

    The sentinel file is created once and never unlinked; the advisory lock
    provides the mutual exclusion across processes.
    """
    locks = state_root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with operation_file_lock(locks / f"{receipt_id}.lock", require_exclusive=True):
        yield


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def _resolve_start_identity(
    mode: str, view: PullRequestView, dependency: PullRequestView | None
) -> dict[str, str]:
    """Return the exact start identity required by the creation mode."""
    if mode == MODE_IN_PLACE:
        return {"object": view.head_sha, "ref": view.head_ref, "repository": view.head_repository}
    if mode == MODE_CLEAN_REBUILD:
        return {"object": view.base_sha, "ref": view.base_ref, "repository": view.base_repository}
    if dependency is None:
        _rejected("stacked mode requires --dependency-pr")
    _require_stacked_relationship(view, dependency)
    return {
        "object": dependency.head_sha,
        "ref": dependency.head_ref,
        "repository": dependency.head_repository,
    }


def _require_stacked_relationship(view: PullRequestView, dependency: PullRequestView) -> None:
    """Require the target base to identify the dependency head exactly."""
    if view.base_repository.casefold() != dependency.head_repository.casefold():
        _rejected(
            "declared dependency does not match the target pull request base repository: "
            f"{dependency.head_repository} != {view.base_repository}"
        )
    if view.base_ref != dependency.head_ref:
        _rejected(
            "declared dependency does not match the target pull request base branch: "
            f"{dependency.head_ref} != {view.base_ref}"
        )
    if view.base_sha != dependency.head_sha:
        _rejected(
            "declared dependency does not match the target pull request base tip: "
            f"{dependency.head_sha} != {view.base_sha}"
        )


def _read_live_pull_requests(
    session: RemoteSession,
    target_repository: str,
    number: int,
    dependency_number: int | None,
    *,
    executable: str,
) -> tuple[PullRequestView, PullRequestView | None]:
    """Read the live target and optional dependency pull requests."""
    view = _read_pull_request(session, target_repository, number, executable=executable)
    dependency = None
    if dependency_number is not None:
        dependency = _read_pull_request(
            session, view.base_repository, dependency_number, executable=executable
        )
    return view, dependency


def _fetch_branch_to_ref(
    session: RemoteSession,
    namespace: Path,
    repository: str,
    branch: str,
    target_ref: str,
    expected_object: str,
) -> None:
    """Fetch one exact branch into a private ref and require the recorded object."""
    arguments = [
        "fetch",
        "--no-tags",
        canonical_repository_url(repository),
        f"+refs/heads/{branch}:{target_ref}",
    ]
    result = _remote_git_run(session, namespace, arguments)
    if result.returncode != 0:
        _temporary(
            f"fetch of {repository} branch {branch} failed: "
            f"{(result.stderr or '').strip() or result.returncode}"
        )
    resolved = _git_output(namespace, ["rev-parse", "--verify", f"{target_ref}^{{commit}}"]).strip()
    if resolved != expected_object:
        _temporary(
            f"fetched object for {repository}:{branch} is {resolved}, not the recorded "
            f"{expected_object}; re-run hephaestus-prepare-pr-worktree to re-derive the start point"
        )


def _worktree_gitdir(worktree: Path) -> tuple[Path, tuple[int, int]]:
    """Resolve and identity-check one worktree Git directory."""
    git_file = worktree / ".git"
    if git_file.is_dir() or not git_file.is_file():
        _rejected(f"repair worktree does not hold a .git file: {git_file}")
    content = git_file.read_text(encoding="utf-8").strip()
    if not content.startswith("gitdir: "):
        _rejected(f"repair worktree .git file is malformed: {git_file}")
    gitdir = Path(content.removeprefix("gitdir: "))
    if not gitdir.is_absolute():
        _rejected(f"repair worktree Git directory is not absolute: {gitdir}")
    if not gitdir.is_dir():
        _rejected(f"repair worktree Git directory is missing: {gitdir}")
    return gitdir, _directory_identity(gitdir)


def _prepare_worktree_state(
    namespace: Path, worktree: Path, local_branch: str, start_object: str
) -> dict[str, Any]:
    """Create the receipt-owned worktree and return its strict binding."""
    # The preparation fetch already pinned refs/heads/<local_branch> to the
    # recorded start object in this fresh namespace, so the worktree attaches
    # to that branch instead of creating it a second time.
    result = _git_quiet(namespace, ["worktree", "add", str(worktree), local_branch])
    if result.returncode != 0:
        _rejected(f"cannot create the repair worktree: {result.stderr.strip()}")
    head = _git_output(worktree, ["rev-parse", "--verify", "HEAD"]).strip()
    if head != start_object:
        _rejected(f"repair worktree HEAD is {head}, not the recorded start {start_object}")
    gitdir, identity = _worktree_gitdir(worktree)
    return {
        "gitdir": str(gitdir),
        "gitdir_identity": [identity[0], identity[1]],
        "local_branch": local_branch,
        "path": str(worktree),
    }


def _prepare_pr(arguments: argparse.Namespace) -> dict[str, Any]:
    """Run the complete preparation contract and return the success payload."""
    repository_root = _validated_repository_root(arguments.repository_root)
    state_root = _validated_build_subdirectory(
        repository_root, str(arguments.state_root), "--state-root"
    )
    worktree_root = _validated_build_subdirectory(
        repository_root, str(arguments.worktree_root), "--worktree-root"
    )
    worktree = _validated_new_worktree_path(worktree_root, str(arguments.worktree))
    validate_pr_identifier(arguments.pull_request)
    number = pull_request_number(arguments.pull_request)
    if arguments.pull_request.startswith("https://"):
        try:
            identifier_repository = repository_from_pr_url(arguments.pull_request, number)
        except RuntimeError as error:
            _rejected(str(error))
        if identifier_repository.casefold() != arguments.target_repository.casefold():
            _rejected("pull request URL does not match --target-repository")
    if (arguments.mode == MODE_STACKED) != (arguments.dependency_pr is not None):
        _rejected("--dependency-pr is required only for stacked mode")
    branch_check = _git_quiet(repository_root, ["check-ref-format", "--branch", arguments.branch])
    if branch_check.returncode != 0:
        _rejected(f"invalid branch name: {arguments.branch}")
    origin, config_digest = _read_source_configuration(repository_root, arguments.target_repository)
    session = remote_session(arguments.gh_extra_path_root, arguments.gh_timeout)
    executable = _resolved_executable(arguments.gh_extra_path_root)
    dependency_number = (
        pull_request_number(arguments.dependency_pr) if arguments.dependency_pr else None
    )
    view, dependency = _read_live_pull_requests(
        session,
        arguments.target_repository,
        number,
        dependency_number,
        executable=executable,
    )
    start = _resolve_start_identity(arguments.mode, view, dependency)
    receipt_id = f"pr{number}-{arguments.mode}-{secrets.token_hex(8)}"
    namespace = _create_namespace(state_root, receipt_id)
    _fetch_branch_to_ref(
        session,
        namespace,
        start["repository"],
        start["ref"],
        f"refs/heads/{arguments.branch}",
        start["object"],
    )
    if view.head_sha != start["object"]:
        _fetch_branch_to_ref(
            session,
            namespace,
            view.head_repository,
            view.head_ref,
            f"{_START_REF_PREFIX}head",
            view.head_sha,
        )
    worktree_binding = _prepare_worktree_state(
        namespace, worktree, arguments.branch, start["object"]
    )
    census = read_open_pull_request_census(session, view.base_repository, executable=executable)
    permitted_refs = (
        f"refs/heads/{arguments.branch}",
        f"{_START_REF_PREFIX}*",
        f"{_CENSUS_REF_PREFIX}*",
    )
    identity, manifest_digest, config_digest = _write_namespace_manifest(namespace, permitted_refs)
    namespace_binding = {
        "config_digest": config_digest,
        "directory_identity": [identity[0], identity[1]],
        "manifest_digest": manifest_digest,
        "path": str(namespace),
        "permitted_refs": sorted(permitted_refs),
    }
    receipt, _digest = _build_preparation_receipt(
        receipt_id=receipt_id,
        target={
            "host": "github.com",
            "number": number,
            "repository": arguments.target_repository,
            "url": view.url,
        },
        view=view,
        mode=arguments.mode,
        start=start,
        dependency=dependency,
        census=census,
        source={
            "config_digest": config_digest,
            "origin_fetch_url": origin,
            "repository_root": str(repository_root),
        },
        namespace=namespace_binding,
        worktree=worktree_binding,
    )
    _validate_receipt_shape(receipt)
    receipts_dir = state_root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts_dir / f"{receipt['receipt_digest']}.json"
    _write_protected_file(
        receipts_dir, receipt_path.name, json.dumps(receipt, indent=2, sort_keys=True)
    )
    _record_outcome(
        state_root,
        receipt,
        state="prepared",
        candidate_object=None,
        message="preparation receipt recorded",
    )
    return {
        "local_branch": arguments.branch,
        "mode": arguments.mode,
        "namespace": str(namespace),
        "receipt": str(receipt_path),
        "start_object": start["object"],
        "worktree": str(worktree),
    }


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


@dataclass
class _PushState:
    """Track whether the push process was dispatched, for interruption readback."""

    attempted: bool = False
    candidate_object: str | None = None
    context: Any = None
    session: RemoteSession | None = None


@dataclass(frozen=True)
class _PublishContext:
    """Everything one publication needs, resolved from the receipt only."""

    receipt: dict[str, Any]
    state_root: Path
    namespace: Path
    worktree: Path
    session: RemoteSession
    executable: str


@dataclass(frozen=True)
class _Outcome:
    """One complete publication outcome."""

    exit_code: int
    outcome: str
    remote_head: str | None
    message: str


def _publish_context(arguments: argparse.Namespace) -> _PublishContext:
    """Resolve the complete publication context from the receipt only."""
    state_root = Path(arguments.state_root).expanduser()
    if not state_root.is_absolute():
        _rejected("--state-root must be an absolute path")
    if not state_root.is_dir():
        _rejected(f"--state-root is not a directory: {state_root}")
    reject_symlinks_below(state_root.parent, state_root)
    receipt = _load_preparation_receipt(Path(arguments.receipt), state_root)
    _bind_target(arguments, receipt)
    _load_namespace_state(receipt)
    session = remote_session(arguments.gh_extra_path_root, arguments.gh_timeout)
    return _PublishContext(
        receipt=receipt,
        state_root=state_root,
        namespace=Path(receipt["namespace"]["path"]),
        worktree=Path(receipt["worktree"]["path"]),
        session=session,
        executable=_resolved_executable(arguments.gh_extra_path_root),
    )


def _resolve_candidate(context: _PublishContext) -> str:
    """Resolve one immutable candidate object from the worktree HEAD."""
    _load_namespace_state(context.receipt)
    gitdir, identity = _worktree_gitdir(context.worktree)
    binding = context.receipt["worktree"]
    if str(gitdir) != binding["gitdir"]:
        _rejected("repair worktree Git directory changed since preparation")
    if identity != tuple(binding["gitdir_identity"]):
        _rejected("repair worktree Git directory identity changed since preparation")
    head = _git_output(context.worktree, ["rev-parse", "--verify", "HEAD"]).strip()
    return _require_oid(head, "repair worktree HEAD")


def _validate_mode_relationship(receipt: dict[str, Any], namespace: Path, candidate: str) -> None:
    """Require the mode-specific start and ancestry relationship."""
    mode = receipt["mode"]
    start_object = receipt["start"]["object"]
    head_sha = receipt["head"]["sha"]
    if mode == MODE_IN_PLACE:
        if start_object != head_sha:
            _rejected("in-place repair does not start from the observed pull request head")
        if not _is_ancestor(namespace, head_sha, candidate):
            _rejected("observed pull request head is not an ancestor of the candidate")
        return
    if mode == MODE_CLEAN_REBUILD:
        base_sha = receipt["base"]["sha"]
        if start_object != base_sha:
            _rejected("clean rebuild does not start from the observed base tip")
        if not _is_ancestor(namespace, base_sha, candidate):
            _rejected("observed base tip is not an ancestor of the candidate")
        if head_sha != base_sha and _is_ancestor(namespace, head_sha, candidate):
            _rejected("prior pull request head became an added ancestor of a clean rebuild")
        return
    dependency = receipt["dependency"]
    if dependency is None:
        _rejected("stacked receipt must record a dependency")
    dependency_head = dependency["head_sha"]
    if start_object != dependency_head:
        _rejected("stacked repair does not start from the recorded dependency head")
    if not _is_ancestor(namespace, dependency_head, candidate):
        _rejected("recorded dependency head is not an ancestor of the candidate")
    if head_sha != dependency_head and _is_ancestor(namespace, head_sha, candidate):
        _rejected("prior pull request head became an added ancestor of a stacked repair")


def _require_view_matches_receipt(view: PullRequestView, receipt: dict[str, Any]) -> None:
    """Require one live pull request to match every receipt-bound field."""
    _require_view_matches_binding(view, receipt["target"], receipt["base"], receipt["head"])


def _require_dependency_matches_receipt(
    dependency: PullRequestView, receipt: dict[str, Any]
) -> None:
    """Require one live dependency pull request to match its receipt binding."""
    binding = PullRequestView.from_flat(receipt["dependency"])
    _require_view_matches_binding(
        dependency,
        {"number": binding.number, "url": binding.url},
        {
            "ref": binding.base_ref,
            "repository": binding.base_repository,
            "sha": binding.base_sha,
        },
        {
            "ref": binding.head_ref,
            "repository": binding.head_repository,
            "sha": binding.head_sha,
        },
    )


def _require_view_matches_binding(
    view: PullRequestView, target: dict[str, Any], base: dict[str, Any], head: dict[str, Any]
) -> None:
    """Require one live pull request to match one strict receipt binding."""
    if view.number != target["number"]:
        _rejected("live pull request number changed since preparation")
    if view.url != target["url"]:
        _rejected("live pull request URL changed since preparation")
    if view.state != "OPEN":
        _rejected("live pull request is not open")
    if view.base_repository.casefold() != base["repository"].casefold():
        _rejected("live base repository changed since preparation")
    if view.base_ref != base["ref"]:
        _rejected("live base branch changed since preparation")
    if view.base_sha != base["sha"]:
        _rejected("live base tip changed since preparation")
    if view.head_repository.casefold() != head["repository"].casefold():
        _rejected("live head repository changed since preparation")
    if view.head_ref != head["ref"]:
        _rejected("live head branch changed since preparation")
    if view.head_sha != head["sha"]:
        _rejected("live head tip changed since preparation")


def _excluded_numbers(receipt: dict[str, Any]) -> frozenset[int]:
    """Return pull-request numbers excluded from the sibling ancestry rule."""
    excluded = {receipt["target"]["number"]}
    dependency = receipt["dependency"]
    if dependency is not None:
        excluded.add(dependency["number"])
    return frozenset(excluded)


def _reject_sibling_ancestry(
    session: RemoteSession,
    namespace: Path,
    candidate: str,
    receipt_census: dict[int, PullRequestView],
    live_census: dict[int, PullRequestView],
    excluded: frozenset[int],
    *,
    executable: str,
) -> None:
    """Fetch receipt-time and live sibling heads and reject ancestry before a push."""
    fetched: dict[tuple[str, str, str], str] = {}
    entries: list[tuple[str, PullRequestView]] = [
        ("receipt-time", view)
        for number, view in sorted(receipt_census.items())
        if number not in excluded
    ]
    entries.extend(
        ("live", view) for number, view in sorted(live_census.items()) if number not in excluded
    )
    for label, view in entries:
        key = (view.head_repository.casefold(), view.head_ref, view.head_sha)
        if key not in fetched:
            census_ref = f"{_CENSUS_REF_PREFIX}{label}-{view.number}"
            _fetch_branch_to_ref(
                session, namespace, view.head_repository, view.head_ref, census_ref, view.head_sha
            )
            fetched[key] = census_ref
        if _is_ancestor(namespace, fetched[key], candidate):
            _rejected(
                f"pull request {view.number} {label} head {view.head_sha} is an ancestor "
                "of the candidate; refusing to publish unrelated ancestry"
            )


def _read_exact_remote_head(
    session: RemoteSession, namespace: Path, repository: str, branch: str
) -> tuple[str, str | None]:
    """Read one exact remote branch head; return a status and its object."""
    arguments = ["ls-remote", canonical_repository_url(repository), f"refs/heads/{branch}"]
    result = _remote_git_run(session, namespace, arguments)
    if result.returncode != 0:
        return "failed", None
    matches = [
        fields[0]
        for fields in (line.split() for line in result.stdout.splitlines())
        if len(fields) >= 2 and fields[1] == f"refs/heads/{branch}"
    ]
    if not matches:
        return "missing", None
    return "ok", matches[0]


def _final_head_gate(
    context: _PublishContext, candidate: str, *, reconciled_record: bool
) -> _Outcome | None:
    """Require the exact recorded head tip, or reconcile an unknown record.

    Returns:
        A reconciled or rejected outcome, or None when the push may proceed.

    Raises:
        ContractRejected: When the remote head moved to an unrelated object.
        TemporaryFailure: When the remote head read failed or moved from the
            exact recorded tip before the push.

    """
    receipt = context.receipt
    status, sha = _read_exact_remote_head(
        context.session, context.namespace, receipt["head"]["repository"], receipt["head"]["ref"]
    )
    if status == "missing":
        _rejected("remote pull request head branch no longer exists")
    if status == "failed":
        _temporary("remote head read failed before publication")
    if sha == candidate:
        message = f"confirmed earlier publication of {candidate}"
        _record_outcome(
            context.state_root,
            receipt,
            state="reconciled",
            candidate_object=candidate,
            message=message,
        )
        return _Outcome(_EXIT_PUBLISHED, "reconciled", sha, message)
    if reconciled_record:
        if sha != receipt["expected_remote_head"]:
            _rejected("remote head moved during publication; re-run hephaestus-prepare-pr-worktree")
        return None
    if sha != receipt["expected_remote_head"]:
        _temporary(
            "remote head moved after live pull request validation; refusing to publish "
            "without an exact lease"
        )
    return None


def _push_candidate(context: _PublishContext, candidate: str) -> None:
    """Push the immutable candidate with the exact expected-head lease."""
    receipt = context.receipt
    lease = (
        f"--force-with-lease=refs/heads/{receipt['head']['ref']}:{receipt['expected_remote_head']}"
    )
    url = canonical_repository_url(receipt["head"]["repository"])
    arguments = ["push", lease, url, f"{candidate}:refs/heads/{receipt['head']['ref']}"]

    def validate_launch() -> None:
        """Revalidate the local namespace and candidate immediately before launch."""
        _load_namespace_state(context.receipt)
        current = _git_output(context.worktree, ["rev-parse", "--verify", "HEAD"]).strip()
        if current != candidate:
            _rejected("repair worktree HEAD changed during publication")

    try:
        _remote_git_run(context.session, context.namespace, arguments, pre_launch=validate_launch)
    except GitPreLaunchError as error:
        raise ContractRejectedError(f"launch validation stopped the push: {error}") from error


def _classify_readback(
    status: str, sha: str | None, candidate: str, expected_remote_head: str
) -> tuple[int, str]:
    """Return the exit code and outcome for one post-publication readback."""
    if status == "failed":
        return _EXIT_UNKNOWN, "unknown"
    if sha == candidate:
        return _EXIT_PUBLISHED, "published"
    if sha == expected_remote_head:
        return _EXIT_TEMPORARY, "temporary-failure"
    return _EXIT_REJECTED, "rejected"


def _readback_outcome(context: _PublishContext, candidate: str, *, reconciled: bool) -> _Outcome:
    """Read the exact remote branch and classify the publication outcome."""
    receipt = context.receipt
    status, sha = _read_exact_remote_head(
        context.session, context.namespace, receipt["head"]["repository"], receipt["head"]["ref"]
    )
    code, outcome = _classify_readback(status, sha, candidate, receipt["expected_remote_head"])
    if code == _EXIT_PUBLISHED and reconciled:
        outcome = "reconciled"
    if outcome == "published":
        message = f"published {candidate} to {receipt['head']['repository']}"
    elif outcome == "reconciled":
        message = f"confirmed earlier publication of {candidate}"
    elif outcome == "temporary-failure":
        message = "push did not change the remote head; the exact lease preserved the remote"
    elif outcome == "unknown":
        message = "push may have reached the server but the remote head cannot be read"
    else:
        message = "remote head moved during publication; re-run hephaestus-prepare-pr-worktree"
    return _Outcome(code, outcome, sha, message)


def _run_publication(context: _PublishContext, push_state: _PushState) -> _Outcome:
    """Run the complete locked publication contract."""
    receipt = context.receipt
    state_root = context.state_root
    with _hold_receipt_lock(state_root, receipt["receipt_id"]):
        record = _read_outcome_record(state_root, receipt)
        reconciled_record = record is not None and record["state"] == "unknown"
        candidate = _resolve_candidate(context)
        _validate_mode_relationship(receipt, context.namespace, candidate)
        dependency_number = (
            receipt["dependency"]["number"] if receipt["dependency"] is not None else None
        )
        view, dependency = _read_live_pull_requests(
            context.session,
            receipt["target"]["repository"],
            receipt["target"]["number"],
            dependency_number,
            executable=context.executable,
        )
        _require_view_matches_receipt(view, receipt)
        if dependency is not None:
            _require_dependency_matches_receipt(dependency, receipt)
            _require_stacked_relationship(view, dependency)
        live_census = read_open_pull_request_census(
            context.session, view.base_repository, executable=context.executable
        )
        receipt_census = {
            int(key): PullRequestView.from_flat(row) for key, row in receipt["census"].items()
        }
        _reject_sibling_ancestry(
            context.session,
            context.namespace,
            candidate,
            receipt_census,
            live_census,
            _excluded_numbers(receipt),
            executable=context.executable,
        )
        _record_outcome(
            state_root,
            receipt,
            state="preflight-complete",
            candidate_object=candidate,
            message="local preflight completed before the push",
        )
        view, dependency = _read_live_pull_requests(
            context.session,
            receipt["target"]["repository"],
            receipt["target"]["number"],
            dependency_number,
            executable=context.executable,
        )
        _require_view_matches_receipt(view, receipt)
        if dependency is not None:
            _require_dependency_matches_receipt(dependency, receipt)
            _require_stacked_relationship(view, dependency)
        early = _final_head_gate(context, candidate, reconciled_record=reconciled_record)
        if early is not None:
            return early
        push_state.attempted = True
        push_state.candidate_object = candidate
        _push_candidate(context, candidate)
        outcome = _readback_outcome(context, candidate, reconciled=reconciled_record)
        _record_outcome(
            state_root,
            receipt,
            state=outcome.outcome,
            candidate_object=candidate,
            message=outcome.message,
        )
        return outcome


# ---------------------------------------------------------------------------
# Output envelopes and console boundaries
# ---------------------------------------------------------------------------


class _ArgumentError(Exception):
    """One invalid console argument reported with the same outcome envelope."""


class _OutcomeParser(argparse.ArgumentParser):
    """Report argument errors through the shared outcome envelope."""

    def error(self, message: str) -> NoReturn:
        """Raise the typed argument error instead of writing through argparse."""
        raise _ArgumentError(text(message)) from None


_RESERVED_FIELDS = frozenset({"exit_code", "message", "outcome", "receipt", "status"})


def _emit_outcome(
    json_mode: bool,
    *,
    exit_code: int,
    outcome: str,
    message: str,
    receipt: str | None = None,
    **extra: Any,
) -> None:
    """Write one outcome envelope on the caller's requested output channel."""
    reserved = _RESERVED_FIELDS.intersection(extra)
    if reserved:
        raise ValueError(f"outcome fields cannot replace reserved fields: {sorted(reserved)}")
    if json_mode:
        envelope: dict[str, Any] = {
            "exit_code": exit_code,
            "message": message,
            "outcome": outcome,
            "receipt": receipt,
            "status": "ok" if exit_code == 0 else "error",
        }
        envelope.update(extra)
        print(json.dumps(envelope, sort_keys=True))
        return
    if exit_code == 0:
        print(message)
    else:
        print(message, file=sys.stderr)


def _json_requested(arguments: Sequence[str]) -> bool:
    """Detect the literal ``--json`` option before full parsing."""
    return "--json" in arguments


def _argument_outcome(arguments: Sequence[str], error: _ArgumentError) -> int:
    """Emit one argument-error outcome on the caller's requested channel."""
    _emit_outcome(
        _json_requested(arguments),
        exit_code=_EXIT_ARGUMENT_ERROR,
        outcome="argument-error",
        message=str(error),
        receipt=None,
    )
    return _EXIT_ARGUMENT_ERROR


def _prepare_parser() -> _OutcomeParser:
    """Build the strict preparation parser."""
    parser = _OutcomeParser(prog="hephaestus-prepare-pr-worktree", description=__doc__)
    add_version_arg(parser)
    parser.add_argument("pull_request", metavar="PR_NUMBER_OR_URL")
    parser.add_argument("--target-host", required=True, metavar="HOST")
    parser.add_argument("--target-repository", required=True, metavar="OWNER/REPOSITORY")
    parser.add_argument("--repository-root", required=True, type=Path, metavar="PATH")
    parser.add_argument("--state-root", required=True, type=Path, metavar="PATH")
    parser.add_argument("--worktree", required=True, type=Path, metavar="PATH")
    parser.add_argument("--worktree-root", required=True, type=Path, metavar="PATH")
    parser.add_argument("--branch", required=True, metavar="LOCAL_BRANCH")
    parser.add_argument("--mode", required=True, choices=_CREATION_MODES, metavar="MODE")
    parser.add_argument("--dependency-pr", metavar="PR_NUMBER_OR_URL")
    add_gh_extra_path_root_arg(parser)
    parser.add_argument(
        "--gh-timeout", type=positive_timeout, default=_DEFAULT_GH_TIMEOUT, metavar="SECONDS"
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    return parser


def _publish_parser() -> _OutcomeParser:
    """Build the strict publication parser."""
    parser = _OutcomeParser(prog="hephaestus-publish-pr-worktree", description=__doc__)
    add_version_arg(parser)
    parser.add_argument("pull_request", metavar="PR_NUMBER_OR_URL")
    parser.add_argument("--target-host", required=True, metavar="HOST")
    parser.add_argument("--target-repository", required=True, metavar="OWNER/REPOSITORY")
    parser.add_argument("--state-root", required=True, type=Path, metavar="PATH")
    parser.add_argument("--receipt", required=True, type=Path, metavar="PATH")
    add_gh_extra_path_root_arg(parser)
    parser.add_argument(
        "--gh-timeout", type=positive_timeout, default=_DEFAULT_GH_TIMEOUT, metavar="SECONDS"
    )
    add_github_throttle_args(parser)
    add_json_arg(parser)
    return parser


def _run_preparation(argv: Sequence[str] | None) -> int:
    """Run the preparation boundary with complete outcome classification."""
    arguments = _argument_list(argv)
    json_mode = _json_requested(arguments)
    try:
        try:
            parsed = _prepare_parser().parse_args(arguments)
        except _ArgumentError as error:
            return _argument_outcome(arguments, error)
        configure_github_throttle_from_args(parsed)
        payload = _prepare_pr(parsed)
        receipt = payload.pop("receipt")
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_PUBLISHED,
            outcome="prepared",
            message=(
                f"prepared pull request {pull_request_number(parsed.pull_request)} repair "
                f"worktree at {payload['worktree']}"
            ),
            receipt=receipt,
            **payload,
        )
        return _EXIT_PUBLISHED
    except ContractRejectedError as error:
        _emit_outcome(json_mode, exit_code=_EXIT_REJECTED, outcome="rejected", message=str(error))
        return _EXIT_REJECTED
    except TemporaryFailureError as error:
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_TEMPORARY,
            outcome="temporary-failure",
            message=str(error),
        )
        return _EXIT_TEMPORARY
    except KeyboardInterrupt:
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_INTERRUPTED,
            outcome="interrupted",
            message="interrupted before any remote write",
        )
        return _EXIT_INTERRUPTED
    except OSError as error:
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_TEMPORARY,
            outcome="temporary-failure",
            message=f"preparation transport failed: {error}",
        )
        return _EXIT_TEMPORARY


def _argument_list(argv: Sequence[str] | None) -> list[str]:
    """Return the caller's argument vector or the process arguments."""
    return list(argv) if argv is not None else sys.argv[1:]


def _interrupted_outcome(push_state: _PushState) -> _Outcome:
    """Classify one operator interruption before or after a possible write."""
    context = push_state.context
    candidate = push_state.candidate_object
    if not push_state.attempted or context is None or candidate is None:
        return _Outcome(
            _EXIT_INTERRUPTED, "interrupted", None, "interrupted before any remote write"
        )
    try:
        outcome = _readback_outcome(context, candidate, reconciled=False)
        _record_outcome(
            context.state_root,
            context.receipt,
            state=outcome.outcome,
            candidate_object=candidate,
            message=outcome.message,
        )
        return outcome
    except (ContractRejectedError, TemporaryFailureError, OSError):
        message = "interrupted after a possible push; the remote head cannot be read"
        return _Outcome(_EXIT_UNKNOWN, "unknown", None, message)


def _run_publication_boundary(argv: Sequence[str] | None) -> int:
    """Run the publication boundary with complete outcome classification."""
    arguments = _argument_list(argv)
    json_mode = _json_requested(arguments)
    push_state = _PushState()
    receipt_for_outcome: str | None = None
    try:
        try:
            parsed = _publish_parser().parse_args(arguments)
        except _ArgumentError as error:
            return _argument_outcome(arguments, error)
        configure_github_throttle_from_args(parsed)
        context = _publish_context(parsed)
        push_state.context = context
        push_state.session = context.session
        receipt_for_outcome = str(Path(parsed.receipt))
        outcome = _run_publication(context, push_state)
        _emit_outcome(
            json_mode,
            exit_code=outcome.exit_code,
            outcome=outcome.outcome,
            message=outcome.message,
            receipt=receipt_for_outcome,
            candidate_object=push_state.candidate_object,
            remote_head=outcome.remote_head,
        )
        return outcome.exit_code
    except ContractRejectedError as error:
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_REJECTED,
            outcome="rejected",
            message=str(error),
            receipt=receipt_for_outcome,
            candidate_object=push_state.candidate_object,
            remote_head=None,
        )
        return _EXIT_REJECTED
    except TemporaryFailureError as error:
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_TEMPORARY,
            outcome="temporary-failure",
            message=str(error),
            receipt=receipt_for_outcome,
            candidate_object=push_state.candidate_object,
            remote_head=None,
        )
        return _EXIT_TEMPORARY
    except KeyboardInterrupt:
        outcome = _interrupted_outcome(push_state)
        _emit_outcome(
            json_mode,
            exit_code=outcome.exit_code,
            outcome=outcome.outcome,
            message=outcome.message,
            receipt=receipt_for_outcome,
            candidate_object=push_state.candidate_object,
            remote_head=None,
        )
        return outcome.exit_code
    except OSError as error:
        _emit_outcome(
            json_mode,
            exit_code=_EXIT_TEMPORARY,
            outcome="temporary-failure",
            message=f"publication transport failed: {error}",
            receipt=receipt_for_outcome,
            candidate_object=push_state.candidate_object,
            remote_head=None,
        )
        return _EXIT_TEMPORARY


def prepare_pr_worktree_main(argv: Sequence[str] | None = None) -> int:
    """Run hephaestus-prepare-pr-worktree through the shared console boundary.

    The boundary reports a failure with one exit code and one stderr line.
    Argument errors use the same outcome envelope with exit code 2.
    """
    return boundary_main(_run_preparation, argv)


def publish_pr_worktree_main(argv: Sequence[str] | None = None) -> int:
    """Run hephaestus-publish-pr-worktree through the shared console boundary.

    The boundary reports a failure with one exit code and one stderr line.
    Argument errors use the same outcome envelope with exit code 2.
    """
    return boundary_main(_run_publication_boundary, argv)


__all__ = [
    "CENSUS_MAX_PAGES",
    "CENSUS_PAGE_SIZE",
    "MODE_CLEAN_REBUILD",
    "MODE_IN_PLACE",
    "MODE_STACKED",
    "RECEIPT_SCHEMA_VERSION",
    "PullRequestView",
    "canonical_repository_url",
    "invalid_branch_reason",
    "prepare_pr_worktree_main",
    "publish_pr_worktree_main",
    "pull_request_number",
    "read_open_pull_request_census",
    "remote_session",
]
