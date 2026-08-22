"""Resolve the trusted Mnemosyne repository under Athena's contract.

Mnemosyne is a repository dependency, not a Pi package.  Resolution follows
Athena's dependency-resolution contract:

1. ``HOMERIC_INTELLIGENCE_MNEMOSYNE_OWNER`` or an explicit ``override_owner``
   is an explicit trust decision.  Invalid explicit owners are fatal.
2. Without an override, a same-owner ``Mnemosyne`` repository is used only when
   the current repository owner is an organization, the viewer can write, and
   GitHub proves it is a fork of ``HomericIntelligence/Mnemosyne``.
3. Otherwise the canonical upstream is selected.  API/auth failures that
   prevent a trustworthy decision are fatal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock
from typing import Any

from hephaestus.github.client import gh_call
from hephaestus.utils.helpers import METADATA_TIMEOUT

logger = logging.getLogger(__name__)

UPSTREAM_OWNER = "HomericIntelligence"
MNEMOSYNE_REPO = "Mnemosyne"
UPSTREAM_SLUG = f"{UPSTREAM_OWNER}/{MNEMOSYNE_REPO}"
OWNER_ENV_VAR = "HOMERIC_INTELLIGENCE_MNEMOSYNE_OWNER"
LEGACY_OWNER_ENV_VAR = "HEPH_MNEMOSYNE_OWNER"

_OWNER_RE = re.compile(r"(?=.{1,39}\Z)[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9]))*")
_WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})
_NOT_FOUND_MARKERS = ("not found", "could not resolve", "404")
_legacy_owner_warning_lock = Lock()
_legacy_owner_warning_emitted = Event()


class MnemosyneResolutionError(RuntimeError):
    """Raised when a trustworthy Mnemosyne target cannot be selected."""


class MnemosyneTrustBasis(StrEnum):
    """Trust basis reported by the dependency resolver."""

    EXPLICIT_OVERRIDE = "explicit override"
    MAINTAINED_ORGANIZATION_FORK = "maintained organization fork"
    CANONICAL_UPSTREAM = "canonical upstream"


@dataclass(frozen=True)
class CurrentRepositoryMetadata:
    """GitHub facts for the repository running Hephaestus automation."""

    owner: str
    owner_type: str
    viewer_permission: str


@dataclass(frozen=True)
class RepositoryMetadata:
    """GitHub facts for one ``owner/Mnemosyne`` candidate."""

    slug: str
    default_branch: str
    head_sha: str
    is_fork: bool = False
    parent_full_name: str = ""


@dataclass(frozen=True)
class MnemosyneTarget:
    """Resolved Mnemosyne repository identity and immutable branch facts."""

    owner: str
    slug: str
    is_fork_of_upstream: bool
    default_branch: str = ""
    head_sha: str = ""
    trust_basis: MnemosyneTrustBasis = MnemosyneTrustBasis.CANONICAL_UPSTREAM


def _slug_for(owner: str) -> str:
    """Return ``owner/Mnemosyne`` for the given owner."""
    return f"{owner}/{MNEMOSYNE_REPO}"


def validate_owner(owner: str) -> str:
    """Return a validated GitHub owner or raise for unsafe explicit input."""
    candidate = owner.strip()
    if _OWNER_RE.fullmatch(candidate) is None:
        raise MnemosyneResolutionError(
            f"invalid Mnemosyne owner {owner!r}; expected a GitHub owner name"
        )
    return candidate


def _gh_json(args: list[str], *, timeout: int = METADATA_TIMEOUT) -> dict[str, Any]:
    try:
        result = gh_call(
            args,
            check=False,
            log_on_error=False,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        raise MnemosyneResolutionError(
            f"GitHub query failed for gh {' '.join(args)}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise MnemosyneResolutionError(
            f"GitHub query failed for gh {' '.join(args)}: {detail or result.returncode}"
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MnemosyneResolutionError("GitHub query returned malformed JSON") from exc
    if not isinstance(data, dict):
        raise MnemosyneResolutionError("GitHub query returned non-object JSON")
    return data


def _repo_view_json(slug: str | None = None) -> dict[str, Any]:
    args = ["repo", "view"]
    if slug is not None:
        args.append(slug)
    args.extend(["--json", "owner,viewerPermission,defaultBranchRef,isFork,parent"])
    return _gh_json(args)


def fetch_current_repository_metadata() -> CurrentRepositoryMetadata:
    """Return current repository owner and viewer-permission facts."""
    data = _repo_view_json()
    owner = data.get("owner")
    if not isinstance(owner, dict):
        raise MnemosyneResolutionError("current repository metadata lacks owner")
    login = owner.get("login")
    owner_type = owner.get("type")
    permission = data.get("viewerPermission")
    if not isinstance(login, str) or not login:
        raise MnemosyneResolutionError("current repository metadata is incomplete")
    login = validate_owner(login)
    if not isinstance(owner_type, str) or not owner_type:
        owner_metadata = _gh_json(["api", f"users/{login}"])
        owner_type = owner_metadata.get("type")
    if not isinstance(owner_type, str) or not owner_type:
        raise MnemosyneResolutionError("current repository metadata is incomplete")
    if not isinstance(permission, str) or not permission:
        raise MnemosyneResolutionError("current repository metadata is incomplete")
    return CurrentRepositoryMetadata(
        owner=login,
        owner_type=owner_type,
        viewer_permission=permission,
    )


def _default_branch_and_head(data: dict[str, Any], slug: str) -> tuple[str, str]:
    default_branch_ref = data.get("defaultBranchRef")
    if not isinstance(default_branch_ref, dict):
        raise MnemosyneResolutionError(f"{slug} metadata lacks defaultBranchRef")
    branch = default_branch_ref.get("name")
    target = default_branch_ref.get("target")
    head = ""
    if isinstance(target, dict):
        raw_head = target.get("oid") or target.get("sha")
        if isinstance(raw_head, str):
            head = raw_head
    raw_oid = default_branch_ref.get("oid")
    if not head and isinstance(raw_oid, str):
        head = raw_oid
    if not isinstance(branch, str) or not branch:
        raise MnemosyneResolutionError(f"{slug} metadata lacks a default branch name")
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        ref = _gh_json(["api", f"repos/{slug}/git/ref/heads/{branch}"])
        obj = ref.get("object")
        raw_head = obj.get("sha") if isinstance(obj, dict) else None
        head = raw_head if isinstance(raw_head, str) else ""
    if re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise MnemosyneResolutionError(f"{slug} metadata lacks a default branch head SHA")
    return branch, head


def fetch_repository_metadata(slug: str, *, missing_ok: bool = False) -> RepositoryMetadata | None:
    """Return GitHub metadata for one candidate dependency repository."""
    try:
        data = _repo_view_json(slug)
    except MnemosyneResolutionError as exc:
        if missing_ok and any(marker in str(exc).casefold() for marker in _NOT_FOUND_MARKERS):
            return None
        raise
    default_branch, head_sha = _default_branch_and_head(data, slug)
    parent = data.get("parent")
    parent_full_name = ""
    if isinstance(parent, dict) and isinstance(parent.get("nameWithOwner"), str):
        parent_full_name = parent["nameWithOwner"]
    return RepositoryMetadata(
        slug=slug,
        default_branch=default_branch,
        head_sha=head_sha,
        is_fork=bool(data.get("isFork")),
        parent_full_name=parent_full_name,
    )


def _target_from_metadata(
    owner: str,
    metadata: RepositoryMetadata,
    trust_basis: MnemosyneTrustBasis,
) -> MnemosyneTarget:
    return MnemosyneTarget(
        owner=owner,
        slug=metadata.slug,
        is_fork_of_upstream=owner != UPSTREAM_OWNER,
        default_branch=metadata.default_branch,
        head_sha=metadata.head_sha,
        trust_basis=trust_basis,
    )


def _canonical_target() -> MnemosyneTarget:
    metadata = fetch_repository_metadata(UPSTREAM_SLUG)
    if metadata is None:  # pragma: no cover - missing_ok is false above
        raise MnemosyneResolutionError(f"{UPSTREAM_SLUG} metadata unavailable")
    return _target_from_metadata(
        UPSTREAM_OWNER,
        metadata,
        MnemosyneTrustBasis.CANONICAL_UPSTREAM,
    )


def gh_authenticated_login(*, timeout: int = METADATA_TIMEOUT) -> str | None:
    """Return the authenticated login for legacy diagnostics.

    The Athena resolver no longer selects user forks based on login, but this
    helper remains for callers and tests that display ``gh`` identity.
    """
    del timeout
    try:
        current = fetch_current_repository_metadata()
    except MnemosyneResolutionError:
        return None
    return current.owner


def remote_repo_exists(slug: str, *, timeout: int = METADATA_TIMEOUT) -> bool:
    """Return True when ``slug`` exists and its metadata is readable."""
    del timeout
    return fetch_repository_metadata(slug, missing_ok=True) is not None


def fork_upstream(owner: str, *, timeout: int = METADATA_TIMEOUT) -> bool:
    """Reject automatic fork creation under Athena's resolution contract."""
    del owner, timeout
    logger.warning("Automatic Mnemosyne fork creation is not permitted")
    return False


def _warn_legacy_owner_once(legacy_owner: str) -> None:
    """Warn once per process when the ignored legacy owner variable is set."""
    with _legacy_owner_warning_lock:
        if _legacy_owner_warning_emitted.is_set():
            return
        _legacy_owner_warning_emitted.set()
    logger.warning(
        "%s is ignored; use %s=%s to make an explicit Mnemosyne trust decision",
        LEGACY_OWNER_ENV_VAR,
        OWNER_ENV_VAR,
        legacy_owner,
    )


def resolve_mnemosyne_target(
    *,
    override_owner: str | None = None,
    allow_fork: bool = True,
) -> MnemosyneTarget:
    """Resolve Mnemosyne with Athena's explicit trust and fallback rules."""
    del allow_fork
    raw_override = override_owner if override_owner is not None else os.environ.get(OWNER_ENV_VAR)
    if raw_override:
        owner = validate_owner(raw_override)
        metadata = fetch_repository_metadata(_slug_for(owner))
        if metadata is None:  # pragma: no cover - missing_ok is false above
            raise MnemosyneResolutionError(f"{owner}/Mnemosyne metadata unavailable")
        return _target_from_metadata(owner, metadata, MnemosyneTrustBasis.EXPLICIT_OVERRIDE)

    if legacy := os.environ.get(LEGACY_OWNER_ENV_VAR):
        _warn_legacy_owner_once(legacy)

    current = fetch_current_repository_metadata()
    if (
        current.owner_type == "Organization"
        and current.viewer_permission in _WRITE_PERMISSIONS
        and current.owner != UPSTREAM_OWNER
    ):
        candidate_slug = _slug_for(current.owner)
        candidate = fetch_repository_metadata(candidate_slug, missing_ok=True)
        if (
            candidate is not None
            and candidate.is_fork
            and candidate.parent_full_name == UPSTREAM_SLUG
        ):
            return _target_from_metadata(
                current.owner,
                candidate,
                MnemosyneTrustBasis.MAINTAINED_ORGANIZATION_FORK,
            )

    return _canonical_target()
