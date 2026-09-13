#!/usr/bin/env python3
"""Tests for the JsonFormatter structured logging formatter."""

import json
import logging
from collections.abc import Callable
from datetime import datetime

import pytest

from hephaestus.cli.localization import Localizer, using_localizer
from hephaestus.logging.formatters import RESERVED_FIELDS, JsonFormatter, _LocalizedFormatter


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


class TestLocalizedFormatterOutput:
    """Tests for plain-log template localization."""

    def test_translates_deferred_template_without_mutating_record(self) -> None:
        """Translate a copied message and keep lazy interpolation and source state."""
        record = logging.LogRecord(
            "test",
            logging.INFO,
            "test.py",
            1,
            "Processed %(count)d files",
            ({"count": 2},),
            None,
        )
        formatter = _LocalizedFormatter(
            "%(message)s",
            localizer=Localizer({"Processed %(count)d files": "Traitement de %(count)d fichiers"}),
        )

        assert formatter.format(record) == "Traitement de 2 fichiers"
        assert record.msg == "Processed %(count)d files"
        assert record.args == {"count": 2}

    def test_keeps_non_string_message_unchanged(self) -> None:
        """Do not translate a non-string logging payload."""
        payload = {"status": "ready"}
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, payload, None, None)

        assert _LocalizedFormatter("%(message)s").format(record) == str(payload)
        assert record.msg is payload

    def test_captures_construction_context_as_fallback(self) -> None:
        """Use the construction catalog when a record has no emission capture."""
        with using_localizer({"Ready": "Prêt"}):
            formatter = _LocalizedFormatter("%(message)s")
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "Ready", None, None)

        assert formatter.format(record) == "Prêt"

    def test_json_formatter_keeps_source_message_with_active_catalog(self) -> None:
        """Keep JSON message content outside the localization boundary."""
        record = logging.LogRecord("test", logging.INFO, "test.py", 1, "Ready", None, None)
        formatter = JsonFormatter()
        expected = json.loads(formatter.format(record))

        with using_localizer({"Ready": "Prêt"}):
            actual = json.loads(formatter.format(record))

        assert actual == expected

    @pytest.mark.parametrize("localized_first", [True, False])
    def test_exception_record_is_isolated_across_plain_and_json_handlers(
        self, localized_first: bool
    ) -> None:
        """Keep one exception record stable in both formatter orders."""
        try:
            raise ValueError("bad value")
        except ValueError as error:
            record = logging.LogRecord(
                "test",
                logging.ERROR,
                "test.py",
                1,
                "Failed %(item)s",
                ({"item": "task"},),
                (ValueError, error, error.__traceback__),
            )
        plain = _LocalizedFormatter(
            "%(message)s",
            localizer=Localizer({"Failed %(item)s": "Échec %(item)s"}),
        )
        machine = JsonFormatter()

        if localized_first:
            plain_output = plain.format(record)
            json_output = machine.format(record)
        else:
            json_output = machine.format(record)
            plain_output = plain.format(record)

        assert plain_output.startswith("Échec task")
        payload = json.loads(json_output)
        assert payload["message"] == "Failed task"
        assert "ValueError: bad value" in payload["exception"]
        assert record.msg == "Failed %(item)s"
        assert record.args == {"item": "task"}
        assert record.exc_text is None


class TestJsonFormatterDetails:
    """Tests for JSON timestamp and message details."""

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

    def test_context_extras_attribute_is_not_special(
        self, formatter: JsonFormatter, make_record: Callable[..., logging.LogRecord]
    ) -> None:
        """``_context_extras`` is not a reserved sentinel.

        If a caller sets it as a plain attribute it appears in the JSON output
        like any other extra. Regression test for issue #795 (dead-branch
        removal).
        """
        record = make_record(extra={"_context_extras": {"nested": "value"}})
        parsed = json.loads(formatter.format(record))
        assert parsed["_context_extras"] == {"nested": "value"}


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
