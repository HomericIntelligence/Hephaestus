"""Check private command output without a provider or a tool process."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.fleet_worker import FleetWorker

pytestmark = pytest.mark.precommit


@pytest.fixture
def worker(tmp_path: Path):
    """Open local receipts without starting the provider."""
    tmp_path = tmp_path.resolve()
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    value = FleetWorker(
        state_dir=tmp_path / "state",
        workspace_root=workspace,
        codex_home=home,
        worker_id="worker-one",
        pool_id="pool-one",
        host_id="host-one",
        generation=1,
        capacity=1,
        allocation_id="allocation-one",
    )
    value.journal.append(
        "session",
        {
            **value.identity,
            "sessionId": "session-one",
            "executionId": "execution-one",
            "taskId": "task-one",
            "agentId": "agent-one",
            "providerThreadId": "thread-one",
            "providerTurnId": "turn-one",
            "activity": "tool_running",
            "outcome": None,
            "released": False,
        },
    )
    yield value
    value.close()


def notification(output: str | None = "résultat ✓\nstderr line\n") -> dict[str, Any]:
    """Return a completed command notification from the pinned protocol."""
    return {
        "method": "item/completed",
        "params": {
            "threadId": "thread-one",
            "turnId": "turn-one",
            "completedAtMs": 123456,
            "item": {
                "type": "commandExecution",
                "id": "item-one",
                "command": "just test",
                "cwd": "/workspace/source",
                "status": "failed",
                "aggregatedOutput": output,
                "exitCode": 7,
                "durationMs": 14,
            },
        },
    }


def export(worker: FleetWorker, output: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Export existing retained bytes without the private worker socket."""
    from hephaestus.automation.fleet_session_output import export_session_output

    worker.close()
    receipt = export_session_output(worker.journal.directory, "session-one", 1, output)
    return json.loads(output.read_bytes()), receipt


def test_completed_command_retains_private_output_after_disposal(worker, tmp_path):
    """Keep actual aggregate bytes after worker disposal, outside the journal."""
    worker._notification(notification())
    root = worker.journal.directory / "session-output"
    assert root.is_dir(), "The completed command output must have private retained storage."
    assert "résultat" not in (worker.journal.directory / "receipts.jsonl").read_text()
    assert "just test" not in json.dumps(worker.journal.events)
    worker.close()
    bundle, receipt = export(worker, tmp_path.resolve() / "result.json")
    item = bundle["items"][0]
    assert item["command"] == "just test"
    assert item["exitCode"] == 7
    assert item["status"] == "failed"
    assert item["output"]["text"] == "résultat ✓\nstderr line\n"
    assert item["output"]["providerTruncated"] is None
    assert item["output"]["captureTruncated"] is False
    assert bundle["capture"]["complete"] is False
    assert bundle["identity"]["allocationId"] == "allocation-one"
    assert (
        receipt["receiptDigest"]
        == hashlib.sha256((tmp_path.resolve() / "result.json").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("text", [None, ""])
def test_null_and_empty_output_are_distinct(worker, tmp_path, text):
    """Keep unavailable output distinct from an observed empty string."""
    worker._notification(notification(text))
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    output = bundle["items"][0]["output"]
    assert output["text"] == text
    assert output["byteCount"] == (None if text is None else 0)
    assert output["sha256"] == (None if text is None else hashlib.sha256(b"").hexdigest())


def test_export_requires_a_stopped_worker_without_changing_capture(worker, tmp_path):
    """Reject a live writer without stopping it or changing its retained state."""
    from hephaestus.automation.fleet_session_output import export_session_output

    worker._notification(notification())
    root = worker.journal.directory
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(BlockingIOError):
        export_session_output(root, "session-one", 1, tmp_path.resolve() / "live.json")
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert not worker.journal.sessions["session-one"].get("outputCaptureUnavailable", False)
    next_item = notification("next output")
    next_item["params"]["item"]["id"] = "item-two"
    worker._notification(next_item)
    bundle, _ = export(worker, tmp_path.resolve() / "stopped.json")
    assert bundle["capture"]["retainedItems"] == 2


@pytest.mark.parametrize(
    "owner", ["thread", "turn", "released", "finished", "worker", "generation"]
)
def test_output_requires_the_current_owner_and_turn(worker, owner):
    """Do not retain a command from another owner or an inactive turn."""
    message = notification()
    session = worker.journal.sessions["session-one"]
    if owner in {"thread", "turn"}:
        message["params"][owner + "Id"] = "different"
    elif owner == "released":
        session["released"] = True
    elif owner == "finished":
        session["outcome"] = "completed"
    elif owner == "worker":
        session["workerId"] = "different"
    else:
        session["generation"] = 2
    worker._notification(message)
    assert not (worker.journal.directory / "session-output").exists()


@pytest.mark.parametrize(
    "field,value",
    [("status", "inProgress"), ("exitCode", True), ("durationMs", -1), ("aggregatedOutput", {})],
)
def test_invalid_command_notifications_do_not_enter_capture_counts(worker, tmp_path, field, value):
    """Count only valid completed command notifications."""
    invalid = notification()
    invalid["params"]["item"][field] = value
    worker._notification(invalid)
    assert not (worker.journal.directory / "session-output").exists()
    worker._notification(notification())
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    assert bundle["capture"]["observedCompletedItems"] == 1


def test_repeated_notification_is_idempotent_across_reopen(worker, tmp_path, monkeypatch):
    """Use retained identity when the same completed item is seen again."""
    from hephaestus.automation import fleet_session_output

    message = notification()
    worker._notification(message)
    worker.close()

    def unavailable(*_args):
        pytest.fail("An exact duplicate must not write capture state.")

    with monkeypatch.context() as storage:
        storage.setattr(fleet_session_output, "_create_file", unavailable)
        storage.setattr(fleet_session_output, "_write_manifest", unavailable)
        fleet_session_output.retain_command(
            worker.journal.directory, worker.journal.sessions["session-one"], message["params"]
        )
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    assert bundle["capture"]["observedCompletedItems"] == 1
    assert bundle["capture"]["retainedItems"] == 1


def test_conflicting_clipped_suffix_blocks_export_and_preserves_original(worker, tmp_path):
    """Detect a changed suffix even when both retained prefixes are equal."""
    text = "x" * (64 * 1024)
    worker._notification(notification(text + "first"))
    worker._notification(notification(text + "second"))
    with pytest.raises(ValueError, match="output_capture_unavailable"):
        export(worker, tmp_path.resolve() / "result.json")
    assert worker.journal.sessions["session-one"]["outputCaptureUnavailable"] is True
    assert worker.journal.events[-1]["event"]["activity"] == "model_working"
    assert not (tmp_path.resolve() / "result.json").exists()


def test_output_clips_at_a_utf8_scalar_boundary(worker, tmp_path):
    """Hash only the retained UTF-8 prefix and report the capture limit."""
    text = "x" * (64 * 1024 - 1) + "€suffix"
    worker._notification(notification(text))
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    output = bundle["items"][0]["output"]
    assert output["text"] == text[: 64 * 1024 - 1]
    assert output["byteCount"] == 64 * 1024 - 1
    assert output["captureTruncated"] is True
    assert output["providerTruncated"] is None
    assert bundle["capture"]["retentionLimited"] is True


@pytest.mark.parametrize("field,size", [("command", 16 * 1024 + 1), ("cwd", 4 * 1024 + 1)])
def test_oversized_metadata_is_counted_as_omitted(worker, tmp_path, field, size):
    """Record retention loss without shortening command metadata."""
    message = notification()
    message["params"]["item"][field] = "x" * size
    worker._notification(message)
    worker._notification(message)
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    assert bundle["items"] == []
    assert bundle["capture"] == {
        "profile": "completed_command_items",
        "complete": False,
        "observedCompletedItems": 1,
        "retainedItems": 0,
        "omittedItems": 1,
        "retentionLimited": True,
    }


def test_item_count_limit_is_reported(worker, tmp_path):
    """Keep a bounded number of immutable item records."""
    for number in range(65):
        message = notification("")
        message["params"]["item"]["id"] = f"item-{number}"
        worker._notification(message)
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    assert len(bundle["items"]) == 64
    assert bundle["capture"]["observedCompletedItems"] == 65
    assert bundle["capture"]["omittedItems"] == 1


def test_aggregate_record_limit_is_reported(worker, tmp_path):
    """Apply the encoded record budget before writing another item."""
    for number in range(20):
        message = notification("x" * 65536)
        message["params"]["item"]["id"] = f"item-{number}"
        worker._notification(message)
    bundle, _ = export(worker, tmp_path.resolve() / "result.json")
    assert 0 < len(bundle["items"]) < 20
    assert bundle["capture"]["observedCompletedItems"] == 20
    assert bundle["capture"]["omittedItems"] == 20 - len(bundle["items"])


def test_observation_ledger_limit_blocks_export(worker, tmp_path, monkeypatch):
    """Stop export when the bounded ledger cannot preserve exact counts."""
    from hephaestus.automation import fleet_session_output

    monkeypatch.setattr(fleet_session_output, "MAX_OBSERVED", 2)
    for number in range(3):
        message = notification()
        message["params"]["item"]["id"] = f"item-{number}"
        worker._notification(message)
    with pytest.raises(ValueError, match="output_capture_unavailable"):
        export(worker, tmp_path.resolve() / "result.json")
    assert worker.journal.events[-1]["event"]["activity"] == "model_working"


def test_capture_io_failure_is_durable_and_does_not_stop_activity(worker, tmp_path, monkeypatch):
    """Prevent export of stale counts after a failed storage update."""
    from hephaestus.automation import fleet_session_output

    worker._notification(notification())

    def unavailable(*_args):
        raise OSError("synthetic storage error with private detail")

    monkeypatch.setattr(fleet_session_output, "_write_manifest", unavailable)
    message = notification()
    message["params"]["item"]["id"] = "item-two"
    worker._notification(message)
    worker.close()
    with pytest.raises(ValueError, match="output_capture_unavailable"):
        export(worker, tmp_path.resolve() / "result.json")
    journal = (worker.journal.directory / "receipts.jsonl").read_text()
    assert "outputCaptureUnavailable" in journal
    assert "private detail" not in journal
    assert "outputCaptureUnavailable" not in json.dumps(worker.journal.events)
    assert worker.journal.events[-1]["event"]["activity"] == "model_working"


@pytest.mark.parametrize("transition", ["new-item", "conflict", "observation-limit"])
def test_incomplete_capture_update_refuses_export_without_a_later_worker_fact(
    worker, tmp_path, monkeypatch, transition
):
    """Reject old counts when a storage update has no completion record."""
    from hephaestus.automation import fleet_session_output

    worker._notification(notification())
    worker.close()
    root = worker.journal.directory
    journal_before = (root / "receipts.jsonl").read_bytes()
    items_before = {
        path: path.read_bytes()
        for path in (root / "session-output").glob("*/*.json")
        if path.name != "capture.json"
    }

    def unavailable(*_args):
        raise OSError("synthetic manifest storage error")

    monkeypatch.setattr(fleet_session_output, "_write_manifest", unavailable)
    message = notification()
    if transition == "conflict":
        message["params"]["item"]["aggregatedOutput"] = "different output"
    else:
        message["params"]["item"]["id"] = "item-two"
    if transition == "observation-limit":
        monkeypatch.setattr(fleet_session_output, "MAX_OBSERVED", 1)
    with pytest.raises(OSError):
        fleet_session_output.retain_command(
            worker.journal.directory, worker.journal.sessions["session-one"], message["params"]
        )
    assert (root / "receipts.jsonl").read_bytes() == journal_before
    assert all(path.read_bytes() == content for path, content in items_before.items())
    with pytest.raises(ValueError, match="output_capture_unavailable"):
        export(worker, tmp_path.resolve() / "result.json")


@pytest.mark.parametrize("field", ["allocationId", "taskId", "executionId", "agentId"])
def test_missing_owner_identity_is_unavailable(worker, field):
    """Do not invent an owner for a provider record."""
    worker.journal.sessions["session-one"][field] = None
    worker._notification(notification())
    assert worker.journal.sessions["session-one"]["outputCaptureUnavailable"] is True
    assert not (worker.journal.directory / "session-output").exists()


def test_changed_retained_record_fails_export(worker, tmp_path):
    """Reject retained bytes that no longer match their identity digest."""
    worker._notification(notification())
    records = list((worker.journal.directory / "session-output").glob("*/*.json"))
    record_path = next(path for path in records if path.name != "capture.json")
    record = json.loads(record_path.read_bytes())
    record["command"] = "changed"
    record_path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="output_record_digest_mismatch"):
        export(worker, tmp_path.resolve() / "result.json")


@pytest.mark.parametrize("mode", ["symlink", "hardlink", "public", "duplicate-key", "extra-field"])
def test_export_rejects_unsafe_or_malformed_retained_files(worker, tmp_path, mode):
    """Use private regular files with an exact JSON schema."""
    worker._notification(notification())
    root = worker.journal.directory / "session-output"
    path = next(path for path in root.glob("*/*.json") if path.name != "capture.json")
    if mode == "symlink":
        retained = path.with_suffix(".retained")
        path.rename(retained)
        path.symlink_to(retained)
    elif mode == "hardlink":
        path.with_suffix(".other-link").hardlink_to(path)
    elif mode == "public":
        path.chmod(0o644)
    elif mode == "duplicate-key":
        original = path.read_bytes()
        path.write_bytes(b'{"command":"duplicate",' + original[1:])
    else:
        value = json.loads(path.read_bytes())
        value["extra"] = True
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        export(worker, tmp_path.resolve() / "result.json")
    assert not (tmp_path.resolve() / "result.json").exists()


def test_export_cli_reads_retained_data_without_socket_or_provider(
    worker, tmp_path, monkeypatch, capsys
):
    """Export from private storage after the worker has closed."""
    from hephaestus.automation import fleet_worker_cli

    worker._notification(notification())
    worker.close()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Export must not start a worker or connect to its socket.")

    monkeypatch.setattr(fleet_worker_cli, "FleetWorker", forbidden)
    monkeypatch.setattr(fleet_worker_cli, "exchange", forbidden)
    output = tmp_path.resolve() / "result.json"
    argv = [
        "export-output",
        "--state-dir",
        str(worker.journal.directory),
        "--session-id",
        "session-one",
        "--generation",
        "1",
        "--output",
        str(output),
    ]
    assert fleet_worker_cli.main(argv) == 0
    receipt = json.loads(capsys.readouterr().out)
    before = output.read_bytes()
    assert receipt["receiptDigest"] == hashlib.sha256(before).hexdigest()
    assert fleet_worker_cli.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "session_output_unavailable"}
    assert output.read_bytes() == before
