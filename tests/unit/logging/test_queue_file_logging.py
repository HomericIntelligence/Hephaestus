"""Queue-backed file logging (#3626)."""

from __future__ import annotations

import logging
import logging.handlers
import queue
import time
from pathlib import Path

from hephaestus.logging.utils import setup_logging


def test_emit_does_not_block_when_file_write_is_slow(tmp_path: Path) -> None:
    """A hung file emit does not block the caller of the logger."""
    slow_queue: queue.Queue = queue.Queue()

    class HangingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            time.sleep(1.5)

    listener = logging.handlers.QueueListener(
        slow_queue, HangingHandler(), respect_handler_level=False
    )
    listener.start()

    test_logger = logging.getLogger("queue_file_hang_test")
    test_logger.handlers.clear()
    test_logger.propagate = False
    test_logger.addHandler(logging.handlers.QueueHandler(slow_queue))
    test_logger.setLevel(logging.INFO)

    started = time.monotonic()
    for _ in range(2):
        test_logger.info("hello-from-test")
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    listener.stop()


def test_marker_reaches_file_through_listener(tmp_path: Path) -> None:
    """A marker record written via setup_logging lands in the configured file."""
    log_file = tmp_path / "queue_test.log"
    root = logging.getLogger()
    preserved = list(root.handlers)
    try:
        setup_logging(level=logging.INFO, log_file=str(log_file))
        marker = "queue-marker-fixture-abc123"
        deadline = time.monotonic() + 4.0
        root.info(marker)
        while time.monotonic() < deadline:
            if log_file.exists() and marker in log_file.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        assert log_file.exists()
        assert marker in log_file.read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in preserved:
            root.addHandler(handler)
