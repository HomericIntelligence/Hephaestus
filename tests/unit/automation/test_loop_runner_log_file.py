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
from hephaestus.logging.utils import setup_logging


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Restore logger state after each test."""
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
    assert "loop-record-20" in lines[0]
    if format_name == "json":
        assert json.loads(lines[0])["message"] == "loop-record-20"


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
    setup_logging(primary_stream=None, log_file=path, json_format=True)
    assert len(logging.getLogger().handlers) == 2
    logging.warning("record")
    assert "record" in (tmp_path / "other.log").read_text()
    assert json.loads(Path(path).read_text())["message"] == "record"


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
