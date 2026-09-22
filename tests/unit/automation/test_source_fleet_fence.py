"""Keep uncertain Fleet work excluded through the existing source receipt."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import source_worktree
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from tests.unit.automation.pipeline.test_fleet_execution import _source


def _fence():
    """Fail at the missing behavior surface without a collection error."""
    fence_type = getattr(source_worktree, "FleetAttemptFence", None)
    assert callable(fence_type), "a Fleet attempt needs a lease-local durable source fence"
    return fence_type("a" * 64)


def test_uncertain_fence_blocks_a_restarted_writer(tmp_path):
    """A new manager cannot acquire or prepare the uncertain source again."""
    fence = _fence()
    manager, binding = _source(tmp_path)
    with manager.acquire(
        binding, allowed_tools="Read,Write,Edit,Glob,Grep,Bash", fleet_fence=fence
    ):
        fence.arm()
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations == (
            "fleet-attempt:" + "a" * 64,
        )
    restarted = SourceWorkspaceManager(manager.repo_root, repository=manager.repository)
    with pytest.raises(SourceWorkspaceError, match="Fleet attempt requires reconciliation"):
        with restarted.acquire(binding, fleet_fence=_fence()):
            pytest.fail("a stable attempt ID cannot grant restart permission")
    with pytest.raises(SourceWorkspaceError, match="Fleet attempt requires reconciliation"):
        restarted.prepare(265, SourceLane.IMPLEMENTATION, binding.revision, branch="265-admitted")
    assert binding.cwd.is_dir()
    assert restarted._read_receipt(265, SourceLane.IMPLEMENTATION).obligations == (
        "fleet-attempt:" + "a" * 64,
    )


def test_confirmed_fence_clears_only_its_matching_obligation(tmp_path):
    """The current lease can finish its own attempt and retain unrelated work."""
    fence = _fence()
    manager, binding = _source(tmp_path)
    manager.add_obligation(265, SourceLane.IMPLEMENTATION, "keep-review-evidence")
    with manager.acquire(binding, fleet_fence=fence):
        fence.arm()
        fence.complete()
        assert manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations == (
            "keep-review-evidence",
        )
    with manager.acquire(binding):
        pass
    with pytest.raises(SourceWorkspaceError, match="Fleet source lease is not active"):
        fence.complete()


@pytest.mark.parametrize("write_first", [False, True])
def test_uncertain_fence_write_never_confirms_admission(tmp_path, write_first):
    """An append that writes and then fails must remain excluded on restart."""
    fence = _fence()
    manager, binding = _source(tmp_path)
    write = manager._write_receipt

    def fail(receipt):
        if write_first:
            write(receipt)
        raise OSError("synthetic receipt write failure")

    with manager.acquire(binding, fleet_fence=fence):
        with patch.object(manager, "_write_receipt", side_effect=fail):
            with pytest.raises(OSError, match="synthetic receipt write failure"):
                fence.arm()
    restarted = SourceWorkspaceManager(manager.repo_root, repository=manager.repository)
    receipt = restarted._read_receipt(265, SourceLane.IMPLEMENTATION)
    assert bool(receipt.obligations) is write_first
    if write_first:
        with pytest.raises(SourceWorkspaceError, match="Fleet attempt requires reconciliation"):
            with restarted.acquire(binding):
                pytest.fail("an uncertain persisted fence must prevent execution")


def test_fence_cannot_clear_a_changed_receipt(tmp_path):
    """A conflicting durable attempt cannot be cleared by the earlier lease."""
    fence = _fence()
    manager, binding = _source(tmp_path)
    with manager.acquire(binding, fleet_fence=fence):
        fence.arm()
        receipt = manager._read_receipt(265, SourceLane.IMPLEMENTATION)
        manager._write_receipt(replace(receipt, obligations=("fleet-attempt:" + "b" * 64,)))
        with pytest.raises(SourceWorkspaceError, match="Fleet attempt fence changed"):
            fence.complete()
    assert manager._read_receipt(265, SourceLane.IMPLEMENTATION).obligations == (
        "fleet-attempt:" + "b" * 64,
    )
