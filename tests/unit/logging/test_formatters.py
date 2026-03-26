#!/usr/bin/env python3
"""Tests for the JsonFormatter structured logging formatter."""

import json
import logging
from collections.abc import Callable
from datetime import datetime

import pytest

from hephaestus.logging.formatters import RESERVED_FIELDS, JsonFormatter


@pytest.fixture()
def formatter() -> JsonFormatter:
    """Return a fresh JsonFormatter instance."""
    return JsonFormatter()


@pytest.fixture()
def make_record() -> Callable[..., logging.LogRecord]:
    """Return a factory that creates a LogRecord with optional extras."""

    def _make(
        msg: str = "test message",
        level: int = logging.INFO,
        name: str = "test.logger",
        exc_info: tuple | None = None,
        extra: dict | None = None,
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=None,
            exc_info=exc_info,
        )
        if extra:
            for k, v in extra.items():
                setattr(record, k, v)
        return record

    return _make


class TestJsonFormatterOutput:
    """Tests for basic JsonFormatter output."""

    def test_output_is_valid_json(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Formatted output must be parseable as JSON."""
        record = make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_standard_fields_present(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """All standard fields must be present in JSON output."""
        record = make_record(msg="hello world", level=logging.WARNING, name="my.logger")
        parsed = json.loads(formatter.format(record))
        assert parsed["message"] == "hello world"
        assert parsed["level"] == "WARNING"
        assert parsed["logger"] == "my.logger"
        assert "timestamp" in parsed

    def test_timestamp_is_iso8601(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Timestamp must be a valid ISO 8601 string."""
        record = make_record()
        parsed = json.loads(formatter.format(record))
        # datetime.fromisoformat will raise on invalid format
        dt = datetime.fromisoformat(parsed["timestamp"])
        assert dt.tzinfo is not None  # must include timezone

    def test_message_with_percent_formatting(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Lazy %s formatting in the message must be resolved."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        parsed = json.loads(formatter.format(record))
        assert parsed["message"] == "hello world"


class TestJsonFormatterExtras:
    """Tests for extra/context field handling."""

    def test_extra_fields_included(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Extra fields set on the record appear in JSON output."""
        record = make_record(extra={"request_id": "abc-123", "service": "keystone"})
        parsed = json.loads(formatter.format(record))
        assert parsed["request_id"] == "abc-123"
        assert parsed["service"] == "keystone"

    def test_reserved_field_collision_prefixed(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Context keys that collide with reserved names get a ctx_ prefix."""
        record = make_record(extra={"level": "custom_value", "message": "override"})
        parsed = json.loads(formatter.format(record))
        # Original reserved fields remain intact
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "test message"
        # Colliding extras are prefixed
        assert parsed["ctx_level"] == "custom_value"
        assert parsed["ctx_message"] == "override"

    def test_non_serializable_value_uses_str(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Non-JSON-serializable extra values fall back to str()."""

        class Custom:
            def __str__(self) -> str:
                return "custom-repr"

        record = make_record(extra={"obj": Custom()})
        parsed = json.loads(formatter.format(record))
        assert parsed["obj"] == "custom-repr"


class TestJsonFormatterExceptions:
    """Tests for exception and stack info serialisation."""

    def test_exception_field_present(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """When exc_info is set, an 'exception' field appears in the JSON."""
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = make_record(exc_info=exc_info)
        parsed = json.loads(formatter.format(record))
        assert "exception" in parsed
        assert "ValueError: boom" in parsed["exception"]
        assert "Traceback" in parsed["exception"]

    def test_no_exception_field_when_no_exc_info(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """No 'exception' field when there is no exception."""
        record = make_record()
        parsed = json.loads(formatter.format(record))
        assert "exception" not in parsed

    def test_stack_info_included(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """Stack info is serialised when present."""
        record = make_record()
        record.stack_info = "Stack (most recent call last):\n  File test.py"
        parsed = json.loads(formatter.format(record))
        assert "stack_info" in parsed
        assert "test.py" in parsed["stack_info"]


class TestReservedFields:
    """Tests for the RESERVED_FIELDS constant."""

    def test_reserved_fields_contains_standard_keys(self) -> None:
        """RESERVED_FIELDS must contain all standard JSON log fields."""
        expected = {"timestamp", "level", "logger", "message", "exception", "stack_info"}
        assert expected == RESERVED_FIELDS
