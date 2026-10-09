"""Repository-aware host verification preparation for PR review."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from hephaestus.automation.host_verification_bootstrap import (
    BOOTSTRAP_ISSUE_NUMBER,
    BOOTSTRAP_PR_NUMBER,
    BootstrapGrantError,
    BootstrapProof,
    BootstrapResolutionStatus,
    bootstrap_proof_from_grant,
    resolve_host_verification_bootstrap,
    validate_changed_file_manifest,
    validate_host_verification_proof,
)

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


def _resolve_host_verification_bootstrap(
    github: Any,
    *,
    comment_id: int | None,
    repository: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    manifest: object,
) -> dict[str, object] | None:
    """Resolve the selected actor-owned grant for one exact review checkout."""
    if comment_id is None:
        return None
    if pr_number != BOOTSTRAP_PR_NUMBER:
        raise BootstrapGrantError("bootstrap is bound to the target PR")
    resolution = resolve_host_verification_bootstrap(
        github.issue_comments(BOOTSTRAP_ISSUE_NUMBER),
        comment_id=comment_id,
        repository=repository,
        issue_number=BOOTSTRAP_ISSUE_NUMBER,
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        manifest=manifest,
    )
    if resolution.status is not BootstrapResolutionStatus.APPROVED or resolution.grant is None:
        raise BootstrapGrantError(
            f"host-verification bootstrap is not approved: {resolution.reason or 'rejected'}"
        )
    return bootstrap_proof_from_grant(resolution.grant).as_dict()


def _validate_host_verification_bootstrap(
    github: Any,
    *,
    proof: object,
    repository: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    manifest: object,
) -> None:
    """Re-read the grant and bind it to the current exact review snapshot."""
    parsed = BootstrapProof.from_dict(proof)
    expected_manifest = validate_changed_file_manifest(manifest)
    if (
        parsed.repository != repository
        or parsed.issue_number != BOOTSTRAP_ISSUE_NUMBER
        or parsed.pr_number != pr_number
        or parsed.head_sha != head_sha
        or parsed.base_sha != base_sha
    ):
        raise BootstrapGrantError("bootstrap proof does not bind the current checkout")
    resolution = validate_host_verification_proof(
        parsed,
        github.issue_comments(BOOTSTRAP_ISSUE_NUMBER),
    )
    if resolution.status is not BootstrapResolutionStatus.APPROVED or resolution.grant is None:
        raise BootstrapGrantError(
            f"host-verification bootstrap is not approved: {resolution.reason or 'rejected'}"
        )
    if resolution.grant.manifest != expected_manifest:
        raise BootstrapGrantError("bootstrap proof manifest changed")


__all__ = [
    "_HOST_VERIFICATION_PROFILE",
    "_payload_host_verification_specs",
    "_prepare_host_checks",
    "_repository_host_verification_profile",
    "_resolve_host_verification_bootstrap",
    "_validate_host_verification_bootstrap",
]
