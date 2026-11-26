"""Tests for bounded host-verification receipts."""

from __future__ import annotations

import pytest

from hephaestus.git.verification.receipt import (
    HostVerificationReceipt,
    parse_host_verification_receipt,
)


def test_parse_accepts_a_complete_receipt() -> None:
    """A closed receipt maps to the typed record."""
    receipt = parse_host_verification_receipt(
        {
            "spec_descr": "implementation_pre_pr_test",
            "ok": False,
            "argv": ["uv", "run", "pytest"],
            "error": "check failed",
        }
    )

    assert receipt is not None
    assert receipt.spec_descr == "implementation_pre_pr_test"
    assert receipt.ok is False
    assert receipt.argv == ("uv", "run", "pytest")
    assert receipt.to_diagnostic() == {
        "receipt_id": "implementation_pre_pr_test",
        "error": "check failed",
    }


def test_parse_rejects_incomplete_and_unknown_fields() -> None:
    """Missing fields and unknown keys produce no receipt."""
    assert parse_host_verification_receipt({"spec_descr": "x", "ok": True}) is None
    assert (
        parse_host_verification_receipt({"spec_descr": "x", "ok": True, "argv": ["uv"], "extra": 1})
        is None
    )
    assert parse_host_verification_receipt("not-a-mapping") is None


def test_parse_bounds_the_retained_error() -> None:
    """A long error is cut to the bounded diagnostic field."""
    receipt = parse_host_verification_receipt(
        {"spec_descr": "x", "ok": False, "argv": ["uv"], "error": "s" * 900}
    )

    assert receipt is not None
    assert receipt.error is not None
    assert len(receipt.error) == 500


def test_receipt_rejects_an_unbounded_error_at_construction() -> None:
    """The typed record refuses an error past its cap."""
    with pytest.raises(ValueError, match="bounded string"):
        HostVerificationReceipt("x", False, ("uv",), "s" * 501)
