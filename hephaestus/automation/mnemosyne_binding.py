"""Bind an available Mnemosyne version to a trusted repository."""

from __future__ import annotations

import logging
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.remote_git import (
    trusted_gh_executable,
    trusted_remote_git_config,
)
from hephaestus.config.child_environments import build_gh_child_env, build_git_child_env
from hephaestus.github.mnemosyne_repo import (
    UPSTREAM_OWNER,
    UPSTREAM_SLUG,
    MnemosyneResolutionError,
    MnemosyneTarget,
    MnemosyneTrustBasis,
    resolve_mnemosyne_target,
)
from hephaestus.utils.helpers import NETWORK_TIMEOUT, run_subprocess

logger = logging.getLogger(__name__)


class MnemosyneBindingError(RuntimeError):
    """Raised when an available checkout cannot be safely bound."""

    def __init__(self, message: str, *, failure_kind: str = "mnemosyne_binding") -> None:
        """Initialize a safe message and a stable failure class."""
        self.failure_kind = failure_kind
        super().__init__(message)


GitRunner = Callable[[Path, tuple[str, ...], int], subprocess.CompletedProcess[str]]
Resolver = Callable[[], MnemosyneTarget]


@dataclass(frozen=True)
class MnemosyneBindingReceipt:
    """Receipt that identifies the available Mnemosyne version."""

    root: str
    repository: str
    default_branch: str
    version: str
    commit_sha: str
    sync_status: str
    trust_basis: str
    athena_contract: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable receipt dictionary."""
        return asdict(self)


def default_mnemosyne_root() -> Path:
    """Return Athena's canonical Mnemosyne checkout path."""
    return Path.home() / ".agent_brain" / "knowledge"


def _run_git(cwd: Path, argv: tuple[str, ...], timeout_s: int) -> subprocess.CompletedProcess[str]:
    return run_subprocess(
        ["git", *argv],
        env=build_git_child_env(),
        cwd=cwd,
        check=False,
        timeout=timeout_s,
        track_process_group=True,
    )


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise MnemosyneBindingError(f"{action} failed: {detail or result.returncode}")
    return (result.stdout or "").strip()


def _require_remote_success(result: subprocess.CompletedProcess[str], action: str) -> str:
    """Require remote Git success without copying child output into the error."""
    if result.returncode != 0:
        raise MnemosyneBindingError(
            f"{action} failed: remote Git transport unavailable",
            failure_kind="remote_git_transport",
        )
    return (result.stdout or "").strip()


def _origin_matches(origin: str, slug: str) -> bool:
    normalized = origin.removesuffix(".git")
    return normalized in {
        f"https://github.com/{slug}",
        f"ssh://git@github.com/{slug}",
        f"git@github.com:{slug}",
    }


def _unsafe_config_key(config: str) -> str | None:
    """Return an unsafe effective Git configuration key."""
    for entry in config.split("\0"):
        key, separator, _value = entry.partition("\n")
        if not separator:
            continue
        normalized = key.lower()
        if normalized.startswith(("alias.", "include.", "includeif.", "http.", "url.")):
            return key
        if normalized in {
            "core.askpass",
            "core.fsmonitor",
            "core.gitproxy",
            "core.hookspath",
            "core.sshcommand",
        }:
            return key
        if normalized == "credential.helper" or (
            normalized.startswith("credential.") and normalized.endswith(".helper")
        ):
            return key
        if normalized.startswith("remote.") and normalized.rsplit(".", 1)[-1] in {
            "proxy",
            "proxyauthmethod",
            "pushurl",
            "receivepack",
            "uploadpack",
        }:
            return key
    return None


def _gh_auth_status(command: str, timeout_s: int) -> bool:
    """Return true when the trusted GitHub CLI has a login for GitHub.com."""
    try:
        result = run_subprocess(
            [command, "auth", "status", "--hostname", "github.com"],
            env=build_gh_child_env(),
            check=False,
            timeout=timeout_s,
            track_process_group=True,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class MnemosyneBindingService:
    """Validate and bind an available Mnemosyne checkout before use."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        resolver: Resolver | None = None,
        git: GitRunner = _run_git,
        timeout_s: int = NETWORK_TIMEOUT,
        gh_extra_path_root: Path | None = None,
        remote_git_config: tuple[str, ...] | None = None,
    ) -> None:
        """Initialize the service with injectable resolver and Git runner."""
        self.root = root if root is not None else default_mnemosyne_root()
        self.resolver = resolver if resolver is not None else resolve_mnemosyne_target
        self.git = git
        self.timeout_s = timeout_s
        self.gh_extra_path_root = gh_extra_path_root
        self._remote_git_config = remote_git_config

    def bind(
        self, *, contract: AthenaContractReceipt, sync: bool = True
    ) -> MnemosyneBindingReceipt:
        """Return a receipt for a safe available Mnemosyne version.

        A remote synchronization attempt is best-effort. A usable local
        version remains available when the remote update cannot complete.
        """
        root = self.root
        if root.is_symlink():
            raise MnemosyneBindingError(f"Mnemosyne checkout must not be a symlink: {root}")
        checkout_exists = root.exists()
        if checkout_exists:
            if not root.is_dir():
                raise MnemosyneBindingError(f"Mnemosyne checkout must be a directory: {root}")
            self._validate_git_checkout(root)
            self._validate_safe_config(root)
            self._validate_clean(root)
            target = self._resolve_existing_target(root)
        else:
            target = self.resolver()
            self._clone_missing_checkout(root, target)
        if root.is_symlink() or not root.is_dir():
            raise MnemosyneBindingError(f"Mnemosyne checkout must be a directory: {root}")
        if not checkout_exists:
            self._validate_git_checkout(root)
            self._validate_safe_config(root)
            self._validate_clean(root)
        self._validate_origin(root, target)
        sync_status = "not_requested"
        if sync:
            try:
                self._fast_forward(root, target)
            except (
                MnemosyneBindingError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                failure_kind = getattr(exc, "failure_kind", "mnemosyne_sync")
                logger.warning(
                    "Mnemosyne synchronization did not complete; "
                    "the available local version will be used (failure_kind=%s)",
                    failure_kind,
                )
                sync_status = "not_updated"
            else:
                sync_status = "updated"
            self._validate_clean(root)
        commit = self._head(root)
        version = self._version(root, commit)
        return MnemosyneBindingReceipt(
            root=str(root),
            repository=target.slug,
            default_branch=target.default_branch,
            commit_sha=commit,
            trust_basis=target.trust_basis.value,
            athena_contract=contract.to_dict(),
            version=version,
            sync_status=sync_status,
        )

    def _resolve_existing_target(self, root: Path) -> MnemosyneTarget:
        """Resolve a target or use a verified canonical local checkout."""
        try:
            return self.resolver()
        except MnemosyneResolutionError:
            origin = _require_success(
                self._git(root, "config", "--get", "remote.origin.url"),
                "origin read",
            )
            if not _origin_matches(origin, UPSTREAM_SLUG):
                raise
            logger.warning(
                "Mnemosyne repository resolution did not complete; "
                "the canonical local checkout will be used"
            )
            return MnemosyneTarget(
                owner=UPSTREAM_OWNER,
                slug=UPSTREAM_SLUG,
                is_fork_of_upstream=False,
                default_branch="main",
                trust_basis=MnemosyneTrustBasis.CANONICAL_UPSTREAM,
            )

    def _git(self, root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return self.git(root, tuple(argv), self.timeout_s)

    def _remote_config(self) -> tuple[str, ...]:
        """Return trusted remote configuration or fail closed in production."""
        if self._remote_git_config is not None:
            return self._remote_git_config
        gh_command = trusted_gh_executable(self.gh_extra_path_root)
        if gh_command is None:
            raise MnemosyneBindingError(
                "remote Git authentication unavailable: trusted GitHub executable missing",
                failure_kind="remote_git_authentication",
            )
        if not _gh_auth_status(gh_command, self.timeout_s):
            raise MnemosyneBindingError(
                "remote Git authentication unavailable: GitHub login missing",
                failure_kind="remote_git_authentication",
            )
        remote_config = trusted_remote_git_config(gh_command)
        if remote_config is None:
            raise MnemosyneBindingError(
                "remote Git authentication unavailable: trusted SSH executable missing",
                failure_kind="remote_git_authentication",
            )
        return remote_config

    def _remote_git(self, root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        """Run a remote Git command with command-scoped authentication."""
        return self._git(root, *self._remote_config(), *argv)

    def _clone_missing_checkout(self, root: Path, target: MnemosyneTarget) -> None:
        """Create the canonical parent and clone the already-resolved target.

        The clone remains untrusted until the origin, configuration,
        cleanliness, commit, and version checks in :meth:`bind` complete.
        """
        parent = root.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise MnemosyneBindingError(
                f"cannot create Mnemosyne checkout parent: {parent}"
            ) from exc
        if parent.is_symlink() or not parent.is_dir():
            raise MnemosyneBindingError(
                f"Mnemosyne checkout parent must not be a symlink: {parent}"
            )
        _require_remote_success(
            self._remote_git(
                parent,
                "clone",
                "--origin",
                "origin",
                "--branch",
                target.default_branch,
                f"https://github.com/{target.slug}.git",
                str(root),
            ),
            "clone",
        )

    def _validate_git_checkout(self, root: Path) -> None:
        output = _require_success(
            self._git(root, "rev-parse", "--is-inside-work-tree"),
            "git checkout validation",
        )
        if output != "true":
            raise MnemosyneBindingError(f"not a Git work tree: {root}")

    def _validate_origin(self, root: Path, target: MnemosyneTarget) -> None:
        origin = _require_success(
            self._git(root, "config", "--get", "remote.origin.url"),
            "origin read",
        )
        if not _origin_matches(origin, target.slug):
            raise MnemosyneBindingError(
                f"wrong origin: expected {target.slug}",
                failure_kind="remote_git_identity",
            )

    def _validate_safe_config(self, root: Path) -> None:
        result = self._git(root, "config", "--null", "--list")
        if result.returncode != 0:
            raise MnemosyneBindingError("Git configuration scan failed")
        if _unsafe_config_key(result.stdout or "") is not None:
            raise MnemosyneBindingError("unsafe Git configuration")

    def _validate_clean(self, root: Path) -> None:
        status = _require_success(self._git(root, "status", "--porcelain"), "status read")
        if status:
            raise MnemosyneBindingError("dirty Mnemosyne checkout")

    def _fast_forward(self, root: Path, target: MnemosyneTarget) -> None:
        _require_remote_success(self._remote_git(root, "fetch", "origin"), "fetch")
        _require_success(self._git(root, "checkout", target.default_branch), "checkout")
        _require_success(
            self._git(root, "merge", "--ff-only", f"origin/{target.default_branch}"),
            "fast-forward",
        )

    def _head(self, root: Path) -> str:
        head = _require_success(self._git(root, "rev-parse", "HEAD"), "HEAD read")
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise MnemosyneBindingError(f"invalid HEAD SHA: {head}")
        return head

    def _version(self, root: Path, commit: str) -> str:
        """Read and validate the release version from the available commit."""
        result = self._git(root, "show", f"{commit}:pyproject.toml")
        if result.returncode != 0:
            raise MnemosyneBindingError("Mnemosyne version metadata is unavailable")
        try:
            metadata = tomllib.loads(result.stdout or "")
        except tomllib.TOMLDecodeError as exc:
            raise MnemosyneBindingError("Mnemosyne version metadata is invalid") from exc
        project = metadata.get("project")
        if not isinstance(project, dict):
            raise MnemosyneBindingError("Mnemosyne project metadata is invalid")
        name = project.get("name")
        version = project.get("version")
        if not isinstance(name, str) or canonicalize_name(name) != "project-mnemosyne":
            raise MnemosyneBindingError("Mnemosyne project identity is invalid")
        if not isinstance(version, str) or not version or version != version.strip():
            raise MnemosyneBindingError("Mnemosyne version metadata is invalid")
        try:
            Version(version)
        except InvalidVersion as exc:
            raise MnemosyneBindingError("Mnemosyne version metadata is invalid") from exc
        return version
