#!/usr/bin/env python3
"""Integration tests for setup_logging() and get_logger() interaction.

Exercises the real combined workflow to catch regressions in handler
deduplication and propagation behaviour.
"""

import logging
import sys

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_root_logger():
    """Save and restore root logger state around each test."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)


class TestSetupLoggingThenGetLogger:
    """Integration: setup_logging() called before get_logger()."""

    def test_no_duplicate_output_after_setup_then_get(self, capsys) -> None:
        """get_logger() called after setup_logging() produces exactly one output line."""
        from hephaestus.logging.utils import get_logger, setup_logging

        setup_logging(level=logging.DEBUG)
        logger = get_logger("test.dedup.setup_first", level=logging.DEBUG)

        logger.info("hello-setup-first")

        out = capsys.readouterr().out
        assert out.count("hello-setup-first") == 1, (
            f"Expected exactly one occurrence, got:\n{out!r}"
        )

    def test_get_logger_after_setup_respects_level(self, capsys) -> None:
        """get_logger() after setup_logging(WARNING) does not output DEBUG messages."""
        from hephaestus.logging.utils import get_logger, setup_logging

        setup_logging(level=logging.WARNING)
        logger = get_logger("test.level.setup_first", level=logging.WARNING)

        logger.debug("should-not-appear")
        logger.warning("should-appear")

        out = capsys.readouterr().out
        assert "should-not-appear" not in out
        assert "should-appear" in out


class TestGetLoggerThenSetupLogging:
    """Integration: get_logger() called before setup_logging()."""

    def test_no_duplicate_output_after_get_then_setup(self, capsys) -> None:
        """setup_logging() called after get_logger() produces exactly one output line."""
        from hephaestus.logging.utils import get_logger, setup_logging

        logger = get_logger("test.dedup.get_first", level=logging.DEBUG)
        setup_logging(level=logging.DEBUG)

        logger.info("hello-get-first")

        out = capsys.readouterr().out
        assert out.count("hello-get-first") == 1, f"Expected exactly one occurrence, got:\n{out!r}"

    def test_setup_logging_stderr_does_not_duplicate(self, capsys) -> None:
        """Calling setup_logging(log_to_stderr=True) twice adds stderr handler only once."""
        from hephaestus.logging.utils import setup_logging

        setup_logging(log_to_stderr=True)
        setup_logging(log_to_stderr=True)

        root = logging.getLogger()
        stderr_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) is sys.stderr
        ]
        assert len(stderr_handlers) == 1, (
            f"Expected exactly one stderr handler, found {len(stderr_handlers)}"
        )


class TestFileLogging:
    """Integration: logging to a file via setup_logging() and get_logger()."""

    def test_setup_logging_writes_to_file(self, tmp_path) -> None:
        """Messages logged after setup_logging(log_file=...) appear in the log file."""
        from hephaestus.logging.utils import setup_logging

        log_file = tmp_path / "app.log"
        setup_logging(level=logging.INFO, log_file=str(log_file))

        root_logger = logging.getLogger()
        root_logger.info("file-logging-test")

        content = log_file.read_text()
        assert "file-logging-test" in content

    def test_get_logger_writes_to_file(self, tmp_path) -> None:
        """Messages logged after get_logger(log_file=...) appear in the log file."""
        from hephaestus.logging.utils import get_logger

        log_file = tmp_path / "named.log"
        logger = get_logger("test.file.named", log_file=str(log_file))

        logger.info("named-logger-file-test")

        content = log_file.read_text()
        assert "named-logger-file-test" in content


class TestJsonFormat:
    """Integration: JSON formatting via HEPHAESTUS_LOG_FORMAT env var."""

    def test_json_format_produces_valid_json(self, capsys, monkeypatch) -> None:
        """With json_format=True, each log line is valid JSON."""
        import json

        from hephaestus.logging.utils import get_logger

        logger = get_logger("test.json.fmt", json_format=True, level=logging.INFO)
        logger.info("json-format-test")

        out = capsys.readouterr().out
        for line in out.strip().splitlines():
            if line:
                parsed = json.loads(line)
                assert "message" in parsed
                assert "level" in parsed
                assert "timestamp" in parsed


class TestContextLogger:
    """Integration: ContextLogger.bind() and with_correlation_id() end-to-end."""

    def test_bound_context_appears_in_output(self, capsys) -> None:
        """Fields bound via .bind() appear in log output."""
        from hephaestus.logging.utils import get_logger

        logger = get_logger("test.ctx.bound", json_format=True, level=logging.DEBUG)
        ctx_logger = logger.bind(request_id="req-abc")
        ctx_logger.info("bound-ctx-message")

        out = capsys.readouterr().out
        assert "req-abc" in out

    def test_correlation_id_auto_generated(self, capsys) -> None:
        """with_correlation_id() generates a UUID and includes it in output."""
        from hephaestus.logging.utils import get_logger

        logger = get_logger("test.ctx.cid", json_format=True, level=logging.DEBUG)
        cid_logger = logger.with_correlation_id()
        cid_logger.info("cid-message")

        out = capsys.readouterr().out
        assert "correlation_id" in out

    def test_explicit_correlation_id_used(self, capsys) -> None:
        """with_correlation_id(cid) uses the provided value."""
        from hephaestus.logging.utils import get_logger

        logger = get_logger("test.ctx.cid.explicit", json_format=True, level=logging.DEBUG)
        cid_logger = logger.with_correlation_id("my-trace-id")
        cid_logger.info("explicit-cid-message")

        out = capsys.readouterr().out
        assert "my-trace-id" in out
