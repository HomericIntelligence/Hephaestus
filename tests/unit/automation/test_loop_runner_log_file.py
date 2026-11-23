"""Test the loop file destination through the command entry point."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import pipeline_cli as loop_runner
from hephaestus.logging import utils as logging_utils
from hephaestus.logging.formatters import JsonFormatter
from hephaestus.logging.utils import setup_logging


def _drain_file_logging(*, keep_listener: bool = False) -> None:
    """Drain the process-wide queue listener before a test reads its file.

    Production callers never wait for a file write. The test must stop the
    listener, because its public ``stop`` joins the queue after every queued
    record. ``keep_listener`` restarts the same handlers for a mid-test format
    change.
    """
    listener = logging_utils._file_log_listener
    if listener is None:
        return
    logging_utils._file_log_listener = None
    handlers = tuple(listener.handlers)
    listener.stop()
    if keep_listener:
        logging_utils._file_log_listener = logging.handlers.QueueListener(
            logging_utils._file_log_queue, *handlers, respect_handler_level=True
        )
        logging_utils._file_log_listener.start()
        return
    for handler in handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and handler in logging_utils._setup_file_handlers
        ):
            logging_utils._setup_file_handlers.discard(handler)
            handler.close()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Restore logger state after each test."""
    _drain_file_logging()
    root = logging.getLogger()
    state = (root.handlers[:], root.level, root.filters[:], root.disabled)
    levels = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root.handlers = []
    try:
        yield
    finally:
        _drain_file_logging()
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level, root.filters, root.disabled = state
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


class TrackingStreamHandler(logging.StreamHandler[io.StringIO]):
    """Record handler closure through the public method."""

    def __init__(self, stream: io.StringIO) -> None:
        """Initialize the stream and closure flag."""
        super().__init__(stream)
        self.was_closed = False

    def close(self) -> None:
        """Record closure and preserve the base handler behavior."""
        self.was_closed = True
        super().close()


class StopAfterLoggingError(RuntimeError):
    """Stop before external dispatch."""


def run_main(monkeypatch: pytest.MonkeyPatch, path: Path, *options: str) -> None:
    """Emit records after logging setup and stop dispatch."""

    def stop(*args: object, **kwargs: object) -> None:
        logger = logging.getLogger("hephaestus.automation.pipeline_cli")
        for level in (logging.DEBUG, logging.INFO, logging.WARNING):
            logger.log(level, "loop-record-%s", level)
        raise StopAfterLoggingError

    monkeypatch.setattr(loop_runner, "resolve_agent", stop)
    with pytest.raises(StopAfterLoggingError):
        loop_runner.main(["--log-file", str(path), *options])
    _drain_file_logging()


def test_main_creates_requested_log_file_and_writes_loop_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check the specified logging contract."""
    path = tmp_path / "exact.log"
    run_main(monkeypatch, path)
    assert "loop-record-20" in path.read_text()


def test_main_replaces_existing_stdout_and_stderr_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check the specified logging contract."""
    streams = [io.StringIO(), io.StringIO()]
    handlers = [TrackingStreamHandler(stream) for stream in streams]
    for handler in handlers:
        logging.getLogger().addHandler(handler)
    run_main(monkeypatch, tmp_path / "loop.log")
    assert all(handler not in logging.getLogger().handlers for handler in handlers)
    assert all(stream.getvalue() == "" for stream in streams)
    assert all(handler.was_closed for handler in handlers)


@pytest.mark.parametrize("format_name", ["text", "json"])
def test_main_file_logging_preserves_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
) -> None:
    """Check the specified logging contract."""
    path = tmp_path / "loop.log"
    run_main(monkeypatch, path, "--log-format", format_name)
    lines = path.read_text().splitlines()
    if format_name == "json":
        messages = [json.loads(line)["message"] for line in lines]
        assert "loop-record-20" in messages
        assert any(message.startswith("Runtime identity:") for message in messages)
    else:
        assert any("loop-record-20" in line for line in lines)
        assert any("Runtime identity:" in line for line in lines)


@pytest.mark.parametrize(
    ("options", "minimum"),
    [([], 20), (["--verbose"], 10), (["--quiet"], 30), (["--verbose", "--quiet"], 30)],
)
def test_main_file_logging_preserves_level_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: list[str],
    minimum: int,
) -> None:
    """Check the specified logging contract."""
    path = tmp_path / "loop.log"
    run_main(monkeypatch, path, *options)
    text = path.read_text()
    for level in (10, 20, 30):
        assert (f"loop-record-{level}" in text) == (level >= minimum)


def test_main_reports_unavailable_log_destination_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check the specified logging contract."""
    dispatch = Mock(side_effect=AssertionError("Unexpected dispatch"))
    monkeypatch.setattr(loop_runner, "resolve_agent", dispatch)
    path = tmp_path / "missing" / "loop.log"
    with pytest.raises(SystemExit) as error:
        loop_runner.main(["--log-file", str(path)])
    assert str(path) in str(error.value)
    assert "permissions" in str(error.value)
    dispatch.assert_not_called()


def test_file_only_preserves_other_files_and_reuses_destination(tmp_path: Path) -> None:
    """Check the specified logging contract."""
    other = logging.FileHandler(tmp_path / "other.log")
    logging.getLogger().addHandler(other)
    path = str(tmp_path / "loop.log")
    setup_logging(primary_stream=None, log_file=path)
    listener = logging_utils._file_log_listener
    assert listener is not None
    owned = listener.handlers[0]
    root = logging.getLogger()
    queue_handler = root.handlers[-1]
    setup_logging(primary_stream=None, log_file=path, json_format=True)
    assert logging_utils._file_log_listener is not None
    assert logging_utils._file_log_listener.handlers == (owned,)
    assert root.handlers == [other, queue_handler]
    logging.warning("record")
    _drain_file_logging()
    assert "record" in (tmp_path / "other.log").read_text()
    assert json.loads(Path(path).read_text())["message"] == "record"


def test_owned_file_can_change_from_json_to_text(tmp_path: Path) -> None:
    """Apply the new format to subsequent records on the same handler."""
    root = logging.getLogger()
    path = tmp_path / "loop.log"
    setup_logging(primary_stream=None, log_file=str(path), json_format=True)
    listener = logging_utils._file_log_listener
    assert listener is not None
    owned = listener.handlers[0]
    queue_handler = root.handlers[0]
    logging.warning("first")
    _drain_file_logging(keep_listener=True)

    setup_logging(primary_stream=None, log_file=str(path), format_string="TEXT %(message)s")
    assert logging_utils._file_log_listener is not None
    assert logging_utils._file_log_listener.handlers == (owned,)
    logging.warning("second")
    _drain_file_logging()

    assert root.handlers == [queue_handler]
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "first"
    assert lines[1] == "TEXT second"


@pytest.mark.parametrize("json_format", [False, True])
def test_owned_file_formatter_updates_after_external_matching_handler(
    tmp_path: Path, json_format: bool
) -> None:
    """Update the owned handler when an external handler shares the path."""
    root = logging.getLogger()
    path = tmp_path / "shared.log"
    setup_logging(primary_stream=None, log_file=str(path), json_format=not json_format)
    listener = logging_utils._file_log_listener
    assert listener is not None
    owned = listener.handlers[0]
    queue_handler = root.handlers[0]
    external = logging.FileHandler(path)
    formatter = logging.Formatter("EXTERNAL %(message)s")
    external.setFormatter(formatter)
    # Keep the synchronous external write ahead of the queued owned write so
    # the two records have a deterministic file order.
    root.removeHandler(queue_handler)
    root.addHandler(external)
    root.addHandler(queue_handler)

    setup_logging(
        primary_stream=None,
        log_file=str(path),
        json_format=json_format,
        format_string="TEXT %(message)s",
    )
    assert logging_utils._file_log_listener is not None
    assert logging_utils._file_log_listener.handlers == (owned,)
    logging.warning("record")
    _drain_file_logging()

    assert root.handlers == [external, queue_handler]
    assert external.formatter is formatter
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == "EXTERNAL record"
    if json_format:
        assert json.loads(lines[1])["message"] == "record"
    else:
        assert lines[1] == "TEXT record"


@pytest.mark.parametrize("replace_owned", [False, True])
@pytest.mark.parametrize("external_json", [False, True])
def test_external_file_formatter_survives_setup_and_path_reuse(
    tmp_path: Path, replace_owned: bool, external_json: bool
) -> None:
    """An external handler retains ownership even at a previously owned path."""
    root = logging.getLogger()
    path = tmp_path / "external.log"
    if replace_owned:
        setup_logging(primary_stream=None, log_file=str(path))
        _drain_file_logging()
        queue_handler = root.handlers[0]
        root.removeHandler(queue_handler)
        queue_handler.close()
    external = logging.FileHandler(path)
    formatter = JsonFormatter() if external_json else logging.Formatter("EXTERNAL %(message)s")
    external.setFormatter(formatter)
    root.addHandler(external)

    setup_logging(primary_stream=None, log_file=str(path), json_format=not external_json)
    assert logging_utils._file_log_listener is None
    logging.warning("record")
    _drain_file_logging()

    assert root.handlers == [external]
    assert external.formatter is formatter
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    if external_json:
        assert json.loads(lines[0])["message"] == "record"
    else:
        assert lines[0] == "EXTERNAL record"


def test_failed_target_keeps_root_handlers(tmp_path: Path) -> None:
    """Check the specified logging contract."""
    handler = TrackingStreamHandler(io.StringIO())
    logging.getLogger().addHandler(handler)
    saved = logging.getLogger().handlers[:]
    with pytest.raises(OSError):
        setup_logging(primary_stream=None, log_file=str(tmp_path / "missing" / "x.log"))
    assert logging.getLogger().handlers == saved
    assert not handler.was_closed


@pytest.mark.parametrize("kwargs", [{}, {"log_file": "unused.log", "log_to_stderr": True}])
def test_file_only_rejects_invalid_destinations(kwargs: dict[str, object]) -> None:
    """Check the specified logging contract."""
    with pytest.raises(ValueError):
        setup_logging(primary_stream=None, **kwargs)  # type: ignore[arg-type]
