"""Check host receipt storage and immutable source validation."""

from collections.abc import Callable
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.stages import pr_review_receipts as receipts
from hephaestus.automation.pipeline.stages.pr_review_receipts import (
    _host_verification_receipt_matches,
)
from hephaestus.automation.pipeline.stages.pr_review_verification import (
    _PYTHON_HOST_VERIFICATION_SPECS,
)
from hephaestus.automation.pipeline.work_item import WorkItem

_HEAD = "a" * 40
_SPEC = _PYTHON_HOST_VERIFICATION_SPECS[0]


@pytest.mark.parametrize("valid_digest", [True, False])
def test_storage_preserves_pyxis_metadata_for_receipt_matching(
    make_work_item: Callable[..., WorkItem], valid_digest: bool
) -> None:
    """Stored metadata must reach the matcher without changing its values."""
    digest = "b" * 64 if valid_digest else "invalid"
    metadata = {
        "container_runtime": "pyxis",
        "container_image": f"/images/sha256-{digest}.sqsh",
        "container_image_sha256": digest,
        "container_image_id": "sha256:" + "c" * 64,
        "container_image_reference": "podman://sha256:" + "c" * 64,
        "containerfile_sha256": "d" * 64,
        "container_source_revision": "e" * 40,
    }
    item = make_work_item(payload={"reviewed_pr_head_sha": _HEAD, "host_verification_receipts": []})
    result = JobResult(
        ok=True,
        value={
            **metadata,
            "head_sha": _HEAD,
            "immutable_source": True,
            "failure_kind": "none",
            "platform": "linux",
            "status": "passed",
        },
    )
    with patch.object(receipts, "_payload_host_verification_specs", return_value=(_SPEC,)):
        receipts.store_host_verification_result(item, result)

    (receipt,) = item.payload["host_verification_receipts"]
    assert {key: receipt.get(key) for key in metadata} == metadata
    assert _host_verification_receipt_matches(receipt, _SPEC, _HEAD) is valid_digest


def test_storage_preserves_unsupported_host_without_creating_a_pass(
    make_work_item: Callable[..., WorkItem],
) -> None:
    """An unsupported Linux result cannot satisfy the host check."""
    item = make_work_item(payload={"reviewed_pr_head_sha": _HEAD, "host_verification_receipts": []})
    result = JobResult(
        ok=False,
        error="unsupported_host_verification_boundary",
        value={
            "head_sha": _HEAD,
            "immutable_source": False,
            "failure_kind": "runner",
            "platform": "linux",
            "status": "skipped",
        },
    )
    with patch.object(receipts, "_payload_host_verification_specs", return_value=(_SPEC,)):
        receipts.store_host_verification_result(item, result)

    (receipt,) = item.payload["host_verification_receipts"]
    assert receipt["status"] == "skipped"
    assert receipt["immutable_source"] is False
    assert not _host_verification_receipt_matches(receipt, _SPEC, _HEAD)
