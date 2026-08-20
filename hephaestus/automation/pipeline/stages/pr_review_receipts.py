"""Platform-aware host-verification receipt normalization and matching."""

from __future__ import annotations

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
    """Return whether *receipt* proves a pass or an authentic platform skip."""
    if isinstance(receipt, dict) and receipt.get("status") == "skipped":
        platform = receipt.get("platform")
        return bool(
            receipt.get("head_sha") == reviewed_head
            and receipt.get("argv") == list(spec.argv)
            and receipt.get("immutable_source") is False
            and receipt.get("ok") is False
            and receipt.get("error") == UNSUPPORTED_HOST_VERIFICATION_ERROR
            and isinstance(platform, str)
            and bool(platform)
            and platform != "darwin"
            and isinstance(receipt.get("stdout_tail"), str)
            and isinstance(receipt.get("stderr_tail"), str)
        )
    return bool(
        isinstance(receipt, dict)
        and receipt.get("head_sha") == reviewed_head
        and receipt.get("argv") == list(spec.argv)
        and receipt.get("immutable_source") is True
        and receipt.get("ok") is True
        and receipt.get("status") in {None, "passed"}
        and receipt.get("platform") in {None, "darwin"}
        and isinstance(receipt.get("stdout_tail"), str)
        and isinstance(receipt.get("stderr_tail"), str)
    )


__all__ = [
    "UNSUPPORTED_HOST_VERIFICATION_ERROR",
    "_host_verification_failure_kind",
    "_host_verification_receipt_matches",
    "_host_verification_result_status",
]
