"""Test durable first-publication facts independently of worker admission."""

from __future__ import annotations

import json
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
from hephaestus.automation import first_publication_recovery
from hephaestus.automation.direct_review_recovery import _write_receipt
from hephaestus.automation.first_publication_recovery import FirstPublicationStore
from hephaestus.automation.pipeline.git_jobs import FirstPublicationRecord
from hephaestus.automation.source_worktree import _PreparationDeadline


def _record(root: Path) -> FirstPublicationRecord:
    """Supply complete facts without implying current source authority."""
    return FirstPublicationRecord(
        operation_id="a" * 32,
        repository="example/project",
        scheduler_repository="project",
        issue_number=9,
        branch="9-publication",
        destination="https://github.com/example/project.git",
        workspace=WorkspaceBinding.source(
            cwd=root / "writer",
            reusable_root=root,
            repository="project",
            ownership_key="project:owned:9:impl",
            item_number=9,
            lane=SourceLane.IMPLEMENTATION,
            revision="b" * 40,
            generation=3,
            detached=False,
        ),
        tree_sha="c" * 40,
        scope_base_sha="d" * 40,
        allowed_paths=("local.txt",),
        phase="publication_intent",
    )


def _store(common: Path) -> FirstPublicationStore:
    """Use a controlled budget with the real protected storage owner."""
    return FirstPublicationStore(
        common, deadline=_PreparationDeadline(10.0, lambda: 0.0, threading.Event())
    )


def test_complete_record_survives_reopen_for_lost_callback(tmp_path: Path) -> None:
    """Completion remains discoverable but cannot replace a different operation."""
    common = tmp_path / "common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    assert store.candidate(9) is None
    assert not (common / "hephaestus-source-workspaces").exists()
    written = store.write(record, expected=None)
    assert written == record
    namespace = common / "hephaestus-source-workspaces/first-publications"
    path = namespace / f"9-{record.operation_id}.json"
    assert stat.S_IMODE(namespace.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_bytes()) == record.to_dict()
    complete = replace(record, phase="complete")
    written = store.write(complete, expected=record)
    assert written == complete
    retained = path.read_bytes()
    reopened = _store(common)
    assert reopened.candidate(9) == complete
    assert reopened.read(9, record.operation_id) == complete
    with pytest.raises(ValueError):
        reopened.write(record, expected=complete)
    with pytest.raises(ValueError):
        reopened.write(replace(record, operation_id="e" * 32), expected=None)
    assert path.read_bytes() == retained


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("operation_id", "../other"),
        ("issue_number", True),
        ("phase", "approved"),
        ("expected_remote", "present"),
        ("destination", "https://github.com/other/project.git"),
        ("tree_sha", "HEAD"),
        ("allowed_paths", ["../other"]),
        ("allowed_paths", ["z.txt", "a.txt"]),
        ("allowed_paths", ["local.txt", "local.txt"]),
        ("unknown_authority", True),
    ],
)
def test_record_rejects_invalid_or_extra_facts(tmp_path: Path, field: str, value: Any) -> None:
    """Retained data cannot expand its closed schema or source scope."""
    payload = _record(tmp_path).to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        FirstPublicationRecord.from_dict(payload)


@pytest.mark.parametrize("after_write", [False, True], ids=["before-write", "after-write"])
@pytest.mark.parametrize("completion", [False, True], ids=["intent", "completion"])
def test_write_failure_retains_actual_disk_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_write: bool, completion: bool
) -> None:
    """An interrupted writer reports failure even if its intent reached disk."""
    common = tmp_path / "common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    expected = record if completion else None
    if completion:
        _store(common).write(record, expected=None)
        record = replace(record, phase="complete")
    write = _write_receipt

    def fail_write(*args: Any, **kwargs: Any) -> None:
        if after_write:
            write(*args, **kwargs)
        raise OSError("controlled durable write failure")

    monkeypatch.setattr(first_publication_recovery, "_write_receipt", fail_write)
    with pytest.raises(OSError, match="controlled durable write failure"):
        _store(common).write(record, expected=expected)
    assert _store(common).candidate(9) == (record if after_write else expected)


@pytest.mark.parametrize(
    ("field", "value"),
    [("generation", True), ("item_number", "9"), ("detached", 0), ("schema_version", True)],
)
def test_nested_source_scalar_types_are_not_coerced(tmp_path: Path, field: str, value: Any) -> None:
    """Malformed source facts cannot become a usable ownership binding."""
    payload = _record(tmp_path).to_dict()
    payload["workspace"][field] = value
    with pytest.raises(ValueError):
        FirstPublicationRecord.from_dict(payload)


def test_conflicting_records_block_without_selecting_one(tmp_path: Path) -> None:
    """A second valid record is an ambiguity, not an ordering decision."""
    common = tmp_path / "common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    store.write(record, expected=None)
    other = replace(record, operation_id="e" * 32)
    namespace = common / "hephaestus-source-workspaces/first-publications"
    path = namespace / f"9-{other.operation_id}.json"
    path.write_text(json.dumps(other.to_dict()), encoding="utf-8")
    path.chmod(0o600)
    retained = path.read_bytes()
    with pytest.raises(ValueError, match="Conflicting"):
        store.candidate(9)
    assert path.read_bytes() == retained
    assert store.read(9, record.operation_id) == record


@pytest.mark.parametrize("completion", [False, True], ids=["intent", "completion"])
def test_readback_mismatch_blocks_and_preserves_written_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion: bool
) -> None:
    """Reject a successful write that stores different operation facts."""
    common = tmp_path / "common"
    common.mkdir(mode=0o700)
    record = _record(tmp_path)
    store = _store(common)
    expected = record if completion else None
    if completion:
        store.write(record, expected=None)
        record = replace(record, phase="complete")
    actual = replace(record, tree_sha="e" * 40)

    def write_changed(directory: Path, descriptor: int, path: Path, content: str) -> None:
        assert json.loads(content) == record.to_dict()
        _write_receipt(
            directory, descriptor, path, json.dumps(actual.to_dict(), sort_keys=True) + "\n"
        )

    monkeypatch.setattr(first_publication_recovery, "_write_receipt", write_changed)
    with pytest.raises(ValueError, match="readback does not match"):
        store.write(record, expected=expected)
    assert _store(common).read(9, record.operation_id) == actual
    assert _store(common).candidate(9) == actual
