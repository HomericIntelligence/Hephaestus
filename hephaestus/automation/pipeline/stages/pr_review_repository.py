"""Repository-aware host verification preparation for PR review."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .pr_review_verification import _host_verification_specs, _HostVerificationSpec

_HOST_VERIFICATION_PROFILE = "host_verification_repository_profile"


def _repository_host_verification_profile(repository_root: Path) -> str | None:
    """Return the fixed host plan supported by an immutable checkout."""
    try:
        with (repository_root / "pyproject.toml").open("rb") as project_file:
            project_config = tomllib.load(project_file)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = project_config.get("project")
    if not isinstance(project, dict):
        return None
    if project.get("name") != "HomericIntelligence-Hephaestus":
        return None
    if (repository_root / "hephaestus").is_dir() and (repository_root / "tests").is_dir():
        return "hephaestus"
    return None


def _prepare_host_checks(
    payload: dict[str, Any], repository_root: Path, reviewed_head: str
) -> tuple[_HostVerificationSpec, ...]:
    """Bind a repository plan or explicit unsupported evidence to the review payload."""
    profile = _repository_host_verification_profile(repository_root)
    payload[_HOST_VERIFICATION_PROFILE] = profile
    verifications = _host_verification_specs(payload.get("pr_diff"), profile=profile)
    requested = _host_verification_specs(payload.get("pr_diff"), profile="hephaestus")
    if profile is None and requested:
        payload["host_verification_receipts"] = [
            {
                "head_sha": reviewed_head,
                "immutable_source": True,
                "reason": "repository_profile_unavailable",
                "status": "unsupported",
            }
        ]
    return verifications


def _payload_host_verification_specs(payload: dict[str, Any]) -> tuple[_HostVerificationSpec, ...]:
    """Rebuild the bound host plan for a later stage transition."""
    return _host_verification_specs(
        payload.get("pr_diff"), profile=payload.get(_HOST_VERIFICATION_PROFILE)
    )


__all__ = [
    "_HOST_VERIFICATION_PROFILE",
    "_payload_host_verification_specs",
    "_prepare_host_checks",
    "_repository_host_verification_profile",
]
