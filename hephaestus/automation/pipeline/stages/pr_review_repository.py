"""Repository-aware host verification preparation for PR review."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .base import (
    Continue,
    Disposition,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _is_confirmed_open_unarmed,
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


def _require_reviewed_unarmed_state(
    item: WorkItem, ctx: StageContext, *, review_wait: str
) -> StepResult | None:
    """Verify that the reviewed PR is open, unarmed, and at the reviewed head."""
    if item.pr is None:
        return StageOutcome(Disposition.FINISH_FAIL, "no_pr")
    pr_state = ctx.github.gh_pr_state(item.pr)
    if pr_state is None:
        return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unavailable")
    if pr_state.get("autoMergeRequest") is not None:
        return StageOutcome(Disposition.BLOCKED, "auto_merge_already_armed")
    if not _is_confirmed_open_unarmed(pr_state):
        return StageOutcome(Disposition.FINISH_FAIL, "pr_state_unverified")
    reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
    live_head = str(pr_state.get("headRefOid") or "")
    if not reviewed_head or not live_head or reviewed_head != live_head:
        item.payload.pop("reviewed_pr_head_sha", None)
        item.payload.pop("reviewed_pr_node_id", None)
        return Continue(next_state=review_wait)
    return None


__all__ = [
    "_HOST_VERIFICATION_PROFILE",
    "_payload_host_verification_specs",
    "_prepare_host_checks",
    "_repository_host_verification_profile",
]
