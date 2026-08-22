"""Regression tests for setup_logging formatter options."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from hephaestus.logging.utils import setup_logging


def test_log_format_environment_is_ignored() -> None:
    """Structured logging is selected only through explicit parameters."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from hephaestus.logging.utils import get_logger; "
                "print(type(get_logger('test.explicit-only').logger.handlers[0].formatter).__name__)"
            ),
        ],
        capture_output=True,
        check=True,
        env={"PATH": os.defpath, "HEPHAESTUS_LOG_FORMAT": "json"},
        text=True,
    )

    assert result.stdout.strip() == "Formatter"


def test_custom_datefmt_is_forwarded_to_formatter() -> None:
    """setup_logging forwards an explicit datefmt to logging.Formatter."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    captured: dict[str, object] = {}

    class FakeFormatter:
        def __init__(
            self,
            fmt: str | None = None,
            datefmt: str | None = None,
        ) -> None:
            captured["fmt"] = fmt
            captured["datefmt"] = datefmt

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("HEPHAESTUS_LOG_FORMAT", raising=False)
            mp.setattr("hephaestus.logging.utils.logging.Formatter", FakeFormatter)
            setup_logging(format_string="%(message)s", datefmt="%H:%M:%S")
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)

    assert captured["fmt"] == "%(message)s"
    assert captured["datefmt"] == "%H:%M:%S"


def test_invalid_primary_stream_raises_value_error() -> None:
    """setup_logging rejects unknown primary stream values."""
    with pytest.raises(ValueError, match="primary_stream"):
        setup_logging(primary_stream="file")  # type: ignore[arg-type]
