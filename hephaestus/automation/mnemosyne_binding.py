"""Bind a local Mnemosyne checkout to a trusted repository revision."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.github.mnemosyne_repo import MnemosyneTarget, resolve_mnemosyne_target
from hephaestus.utils.helpers import NETWORK_TIMEOUT, run_subprocess


class MnemosyneBindingError(RuntimeError):
    """Raised when a checkout cannot be safely bound to a target revision."""


GitRunner = Callable[[Path, tuple[str, ...], int], subprocess.CompletedProcess[str]]
Resolver = Callable[[], MnemosyneTarget]

_UNSAFE_CONFIG_RE = (
    r"^(alias\.|include\.|includeIf\.|core\.hooksPath|core\.fsmonitor|core\.sshCommand)"
)


@dataclass(frozen=True)
class MnemosyneBindingReceipt:
    """Receipt proving a local checkout was bound before use."""

    root: str
    repository: str
    default_branch: str
    commit_sha: str
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


def _origin_matches(origin: str, slug: str) -> bool:
    normalized = origin.removesuffix(".git")
    return bool(
        normalized == slug
        or normalized.endswith(f"/{slug}")
        or normalized.endswith(f":{slug}")
        or normalized == f"https://github.com/{slug}"
        or normalized == f"git@github.com:{slug}"
    )


class MnemosyneBindingService:
    """Validate, synchronize, and bind a Mnemosyne checkout before use."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        resolver: Resolver | None = None,
        git: GitRunner = _run_git,
        timeout_s: int = NETWORK_TIMEOUT,
    ) -> None:
        """Initialize the service with injectable resolver and Git runner."""
        self.root = root if root is not None else default_mnemosyne_root()
        self.resolver = resolver if resolver is not None else resolve_mnemosyne_target
        self.git = git
        self.timeout_s = timeout_s

    def bind(
        self, *, contract: AthenaContractReceipt, sync: bool = True
    ) -> MnemosyneBindingReceipt:
        """Return a binding receipt or fail closed before corpus read/write."""
        target = self.resolver()
        root = self.root
        if root.is_symlink():
            raise MnemosyneBindingError(f"Mnemosyne checkout must not be a symlink: {root}")
        if not root.exists():
            self._clone_missing_checkout(root, target)
        if root.is_symlink() or not root.is_dir():
            raise MnemosyneBindingError(f"Mnemosyne checkout must be a directory: {root}")
        self._validate_git_checkout(root)
        self._validate_origin(root, target)
        self._validate_safe_config(root)
        self._validate_clean(root)
        if sync:
            self._fast_forward(root, target)
        commit = self._head(root)
        if commit != target.head_sha:
            raise MnemosyneBindingError(
                f"revision drift: checkout {commit} does not match resolved {target.head_sha}"
            )
        return MnemosyneBindingReceipt(
            root=str(root),
            repository=target.slug,
            default_branch=target.default_branch,
            commit_sha=commit,
            trust_basis=target.trust_basis.value,
            athena_contract=contract.to_dict(),
        )

    def _git(self, root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return self.git(root, tuple(argv), self.timeout_s)

    def _clone_missing_checkout(self, root: Path, target: MnemosyneTarget) -> None:
        """Create the canonical parent and clone the already-resolved target.

        The clone remains untrusted until the normal origin, config, cleanliness,
        fast-forward, and revision checks in :meth:`bind` complete.
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
        _require_success(
            self._git(
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
                f"wrong origin: expected {target.slug}, found {origin or '<missing>'}"
            )

    def _validate_safe_config(self, root: Path) -> None:
        result = self._git(root, "config", "--local", "--get-regexp", _UNSAFE_CONFIG_RE)
        if result.returncode == 0:
            detail = (result.stdout or "").strip()
            raise MnemosyneBindingError(f"unsafe Git config: {detail}")
        if result.returncode != 1:
            _require_success(result, "unsafe Git config scan")

    def _validate_clean(self, root: Path) -> None:
        status = _require_success(self._git(root, "status", "--porcelain"), "status read")
        if status:
            raise MnemosyneBindingError("dirty Mnemosyne checkout")

    def _fast_forward(self, root: Path, target: MnemosyneTarget) -> None:
        _require_success(self._git(root, "fetch", "origin"), "fetch")
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
