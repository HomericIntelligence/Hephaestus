"""Platform-aware host-verification receipt normalization and matching."""

from __future__ import annotations

from ..host_verification_pyxis import HOST_KEYS, pyxis_receipt_metadata_matches
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


def _unsupported_host_verification_skip_matches(
    receipts: object,
    specs: tuple[_HostVerificationSpec, ...],
    reviewed_head: str,
) -> bool:
    """Return whether the fixed plan has an authenticated Linux bootstrap skip."""
    return bool(
        isinstance(receipts, list)
        and len(receipts) == 1
        and specs
        and _authentic_linux_bootstrap_receipt(receipts[0], specs[0], reviewed_head)
    )


__all__ = [
    "HOST_KEYS",
    "UNSUPPORTED_HOST_VERIFICATION_ERROR",
    "_host_verification_failure_kind",
    "_host_verification_receipt_matches",
    "_host_verification_result_status",
    "_unsupported_host_verification_skip_matches",
]


def _authentic_linux_bootstrap_receipt(
    receipt: object, spec: _HostVerificationSpec, reviewed_head: str
) -> bool:
    """Recognize only the first fixed Linux unsupported-boundary result."""
    return bool(
        isinstance(receipt, dict)
        and spec.descr == "review_python_ruff_check"
        and spec.argv == ("uv", "run", "ruff", "check", "hephaestus/", "tests/")
        and receipt.get("argv") == list(spec.argv)
        and receipt.get("head_sha") == reviewed_head
        and receipt.get("ok") is False
        and receipt.get("status") == "skipped"
        and receipt.get("immutable_source") is False
        and receipt.get("failure_kind") == "runner"
        and receipt.get("platform") == "linux"
        and receipt.get("error") == UNSUPPORTED_HOST_VERIFICATION_ERROR
        and receipt.get("bootstrap_unsupported_result") is True
    )
