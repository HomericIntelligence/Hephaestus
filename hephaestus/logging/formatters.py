#!/usr/bin/env python3
"""JSON log formatter for structured logging.

Provides a ``logging.Formatter`` subclass that outputs each log record as a
single JSON line, suitable for ingestion by log aggregation systems such as
Loki/Promtail in the Argus observability stack.

Usage:
    import logging
    from hephaestus.logging.formatters import JsonFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("my.service")
    logger.addHandler(handler)
    logger.info("hello", extra={"request_id": "abc-123"})
"""

from __future__ import annotations

import copy
import json
import logging
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hephaestus._localization import Localizer

_LOCALIZER_RECORD_ATTR = "_hephaestus_localizer"

# Fields that are reserved for the formatter and cannot be overridden by
# context or extra data.  If a context key collides with one of these, it
# is prefixed with ``ctx_`` to avoid silent data loss.
RESERVED_FIELDS: frozenset[str] = frozenset(
    {"timestamp", "level", "logger", "message", "exception", "stack_info"}
)


class _LocalizedFormatter(logging.Formatter):
    """Translate copied plain-text log message templates.

    The localizer captured at construction is a fallback for manually created
    or already-deferred records.  Normal Hephaestus logging captures the active
    context-local localizer on each ``LogRecord`` when the record is emitted, so
    module-level loggers configured before a catalog is selected can still
    render localized output later without depending on handler re-creation.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        localizer: Localizer | None = None,
    ) -> None:
        """Capture the active localizer for deferred or threaded formatting."""
        super().__init__(fmt, datefmt=datefmt)
        if localizer is None:
            from hephaestus._localization import get_localizer

            localizer = get_localizer()
        self._localizer = localizer

    def format(self, record: logging.LogRecord) -> str:
        """Format a translated shallow copy without mutating the record."""
        copied = copy.copy(record)
        if isinstance(copied.msg, str):
            localizer = getattr(copied, _LOCALIZER_RECORD_ATTR, self._localizer)
            copied.msg = localizer.template(copied.msg)
        return super().format(copied)


class JsonFormatter(logging.Formatter):
    """Formatter that serialises log records to single-line JSON.

    Standard fields included in every record:
    - ``timestamp`` – ISO 8601 UTC timestamp
    - ``level`` – log level name (e.g. ``INFO``)
    - ``logger`` – logger name
    - ``message`` – formatted log message

    Any *extra* dict entries attached to the record (e.g. via
    ``ContextLogger.bind()``) are merged as top-level keys.  If an extra
    key collides with a reserved field name it is automatically prefixed
    with ``ctx_`` so that the original field is never shadowed.

    Exception and stack information, when present, are serialised into
    ``exception`` and ``stack_info`` string fields respectively.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format *record* as a JSON string.

        Args:
            record: The log record to format.

        Returns:
            A single-line JSON string representing the log record.

        """
        log_dict: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge extra/context fields, prefixing collisions with ``ctx_``.
        # ``LoggerAdapter`` (used by ``ContextLogger``) flattens its ``extra``
        # dict onto the record as individual attributes; we recover them by
        # set-differencing against the default ``LogRecord`` attribute names.
        extras = {k: v for k, v in record.__dict__.items() if k not in _DEFAULT_RECORD_ATTRS}

        for key, value in extras.items():
            if key in RESERVED_FIELDS:
                log_dict[f"ctx_{key}"] = value
            else:
                log_dict[key] = value

        if record.exc_info and record.exc_info[0] is not None:
            log_dict["exception"] = "".join(traceback.format_exception(*record.exc_info))

        if record.stack_info:
            log_dict["stack_info"] = record.stack_info

        return json.dumps(log_dict, default=str)


# Set of attribute names present on a default LogRecord so we can detect
# user-supplied extras.  We also exclude ``asctime`` (added by ``formatTime()``)
# and ``stack_info`` (present on all records but handled explicitly above) since
# neither should be promoted to a top-level JSON key via the extras path.
_DEFAULT_RECORD_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, None, None, None).__dict__.keys()
) | {"asctime", "stack_info", _LOCALIZER_RECORD_ATTR}
