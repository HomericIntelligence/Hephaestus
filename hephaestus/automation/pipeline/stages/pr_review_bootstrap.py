"""Bind bootstrap evidence to source-review and label operations."""

import json
from collections.abc import Callable
from typing import Any

from hephaestus.automation.host_verification_bootstrap import (
    BOOTSTRAP_PROOF_KEY,
    BootstrapGrantError,
    BootstrapProof,
    authenticate_bootstrap_grant,
    read_fresh_bootstrap_proof,
    revoke_bootstrap_go,
)

from ..diagnostics import redact_diagnostic_text
from .pr_review_receipts import HOST_KEYS, _authentic_linux_bootstrap_receipt
from .pr_review_threads import (
    Disposition,
    JobResult,
    StageContext,
    StageOutcome,
    StepResult,
    WorkItem,
    _host_verification_failure_kind,
    _host_verification_result_status,
    _issue_number,
    _payload_host_verification_specs,
    is_full_commit_sha,
)
from .pr_review_verification import _HostVerificationSpec


def bootstrap_review_failure(
    item: WorkItem,
    ctx: StageContext,
    failure: Callable[[WorkItem, StageContext, None, str], StepResult],
) -> StepResult | None:
    """Check the exact grant before each source-review job."""
    proof = item.payload.get(BOOTSTRAP_PROOF_KEY)
    revoked = bool(
        BOOTSTRAP_PROOF_KEY in item.payload
        and (
            not isinstance(proof, BootstrapProof)
            or proof.repository != f"{ctx.org}/{item.repo}"
            or proof.issue != item.issue
            or proof.pr != item.pr
            or proof.head_sha != item.payload.get("reviewed_pr_head_sha")
            or proof.base_sha != item.payload.get("reviewed_pr_base_sha")
            or proof.manifest != item.payload.get("review_status_manifest")
            or not read_fresh_bootstrap_proof(proof, ctx.github)
        )
    )

    if revoked:
        return failure(item, ctx, None, "host_verification_bootstrap_revoked")
    return None


def prepare_bootstrap_proof(
    item: WorkItem, ctx: StageContext, comment_id: int, reviewed_head: str
) -> None:
    """Authenticate and store the closed source-review grant."""
    proof = authenticate_bootstrap_grant(
        ctx.github.issue_comments(3006),
        comment_id=comment_id,
        repository=f"{ctx.org}/{item.repo}",
        issue=_issue_number(item),
        pr=item.pr or 0,
        head_sha=reviewed_head,
        base_sha=str(item.payload.get("reviewed_pr_base_sha") or ""),
        manifest=item.payload.get("review_status_manifest"),
    )
    if not read_fresh_bootstrap_proof(proof, ctx.github):
        raise BootstrapGrantError("bootstrap grant changed")
    item.payload[BOOTSTRAP_PROOF_KEY] = proof
    item.payload["host_verification_bootstrap_json"] = json.dumps(
        {
            "permission": "source review only",
            "comment_id": proof.comment_id,
            "head_sha": proof.head_sha,
            "base_sha": proof.base_sha,
            "manifest_sha256": proof.manifest_sha256,
            "local_execution_evidence": False,
        },
        sort_keys=True,
    )


def bootstrap_go_failure(
    item: WorkItem, github: Any, pr_number: int, reviewed_head: str
) -> StageOutcome | None:
    """Remove GO authority when the bound grant is no longer valid."""
    if BOOTSTRAP_PROOF_KEY in item.payload:
        proof = item.payload[BOOTSTRAP_PROOF_KEY]
        if (
            not isinstance(proof, BootstrapProof)
            or proof.head_sha != reviewed_head
            or proof.pr != pr_number
            or proof.issue != item.issue
            or proof.base_sha != item.payload.get("reviewed_pr_base_sha")
            or not read_fresh_bootstrap_proof(proof, github)
        ):
            cleared = revoke_bootstrap_go(github, pr=pr_number, head_sha=reviewed_head)
            item.payload.pop(BOOTSTRAP_PROOF_KEY, None)
            return StageOutcome(
                Disposition.FINISH_FAIL,
                "host_verification_bootstrap_revoked"
                if cleared
                else "host_verification_bootstrap_revocation_unverified",
            )
    return None


def admit_bootstrap_receipt(
    item: WorkItem,
    ctx: StageContext,
    receipts: list[Any],
    verifications: tuple[_HostVerificationSpec, ...],
    reviewed_head: str,
) -> bool:
    """Authenticate a grant only for the exact unsupported Linux receipt."""
    comment_id = getattr(ctx.config, "host_verification_bootstrap_comment_id", None)
    if (
        comment_id is not None
        and len(receipts) == 1
        and verifications
        and _authentic_linux_bootstrap_receipt(receipts[0], verifications[0], reviewed_head)
    ):
        prepare_bootstrap_proof(item, ctx, comment_id, reviewed_head)
        return True
    return False


def is_bootstrap_skip(result: JobResult, value: dict[str, Any], reviewed_head: str) -> bool:
    """Identify the exact failed result without changing its receipt fields."""
    return (
        result.ok is False
        and result.error == "unsupported_host_verification_boundary"
        and value.get("head_sha") == reviewed_head
        and value.get("immutable_source") is False
        and value.get("failure_kind") == "runner"
        and value.get("status") == "skipped"
        and value.get("platform") == "linux"
    )


def store_host_verification_result(item: WorkItem, result: JobResult) -> None:
    """Append a bounded, head-bound receipt from the fixed host plan."""
    specs = _payload_host_verification_specs(item.payload)
    reviewed_head = str(item.payload.get("reviewed_pr_head_sha") or "")
    receipts = item.payload.get("host_verification_receipts")
    if (
        not specs
        or not is_full_commit_sha(reviewed_head)
        or not isinstance(receipts, list)
        or len(receipts) >= len(specs)
    ):
        item.payload.pop("host_verification_receipts", None)
        return
    spec = specs[len(receipts)]
    result_value = result.value if isinstance(result.value, dict) else {}
    status, platform = _host_verification_result_status(
        result.value, result.ok, result.error, reviewed_head
    )
    receipts.append(
        {
            "argv": list(spec.argv),
            "head_sha": reviewed_head,
            "immutable_source": bool(
                isinstance(result.value, dict)
                and result.value.get("head_sha") == reviewed_head
                and result.value.get("immutable_source") is True
            ),
            "failure_kind": _host_verification_failure_kind(result_value),
            "ok": result.ok,
            "error": redact_diagnostic_text(result.error or "")[:500],
            "platform": platform,
            "status": status,
            "stdout_tail": redact_diagnostic_text(result.stdout_tail)[-4000:],
            "stderr_tail": redact_diagnostic_text(result.stderr_tail)[-4000:],
            **{key: value for key in HOST_KEYS if isinstance(value := result_value.get(key), str)},
        }
    )
    if is_bootstrap_skip(result, result_value, reviewed_head):
        receipts[-1]["bootstrap_unsupported_result"] = True
