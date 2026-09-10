"""Store and validate host verification receipts for the reviewed commit."""

from __future__ import annotations

from hephaestus.automation.issue_waves import is_full_commit_sha

from ..diagnostics import redact_diagnostic_text
from ..host_verification_pyxis import HOST_KEYS, pyxis_receipt_metadata_matches
from .base import JobResult, WorkItem
from .pr_review_repository import _payload_host_verification_specs
from .pr_review_verification import _HostVerificationSpec

UNSUPPORTED_HOST_VERIFICATION_ERROR = "unsupported_host_verification_boundary"


def _host_verification_result_status(
    result_value: object,
    result_ok: bool,
    result_error: str | None,
    reviewed_head: str,
) -> tuple[str, str]:
    """Return the validated status and platform for a worker result."""
    value = result_value if isinstance(result_value, dict) else {}
    status = value.get("status")
    if status not in {"passed", "failed", "skipped"}:
        status = "passed" if result_ok else "failed"
    platform = value.get("platform")
    if not isinstance(platform, str) or not platform:
        platform = "darwin" if result_ok else ""
    valid_skip = bool(
        status == "skipped"
        and result_ok is False
        and result_error == UNSUPPORTED_HOST_VERIFICATION_ERROR
        and value.get("head_sha") == reviewed_head
        and value.get("immutable_source") is False
        and platform
        and platform != "darwin"
    )
    return ("failed" if status == "skipped" and not valid_skip else status, platform)


def _host_verification_failure_kind(result_value: dict[object, object]) -> str:
    """Return a known failure kind, defaulting malformed values to runner."""
    failure_kind = result_value.get("failure_kind", "runner")
    return failure_kind if failure_kind in {"none", "runner", "test", "validation"} else "runner"


def _host_verification_receipt_matches(
    receipt: object, spec: _HostVerificationSpec, reviewed_head: str
) -> bool:
    """Return whether *receipt* proves an exact-head host-verification pass."""
    if not isinstance(receipt, dict):
        return False
    platform = receipt.get("platform")
    if platform == "linux":
        return bool(
            receipt.get("head_sha") == reviewed_head
            and receipt.get("argv") == list(spec.argv)
            and receipt.get("immutable_source") is True
            and receipt.get("ok") is True
            and receipt.get("status") == "passed"
            and pyxis_receipt_metadata_matches(receipt)
            and isinstance(receipt.get("stdout_tail"), str)
            and isinstance(receipt.get("stderr_tail"), str)
        )
    return bool(
        receipt.get("head_sha") == reviewed_head
        and receipt.get("argv") == list(spec.argv)
        and receipt.get("immutable_source") is True
        and receipt.get("ok") is True
        and receipt.get("status") in {None, "passed"}
        and receipt.get("platform") in {None, "darwin"}
        and isinstance(receipt.get("stdout_tail"), str)
        and isinstance(receipt.get("stderr_tail"), str)
    )


__all__ = [
    "HOST_KEYS",
    "UNSUPPORTED_HOST_VERIFICATION_ERROR",
    "_host_verification_failure_kind",
    "_host_verification_receipt_matches",
    "_host_verification_result_status",
    "store_host_verification_result",
]


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
