#!/usr/bin/env python3
"""Tests for logging utilities."""

import logging
import threading
from pathlib import Path

from hephaestus.logging.utils import (
    ContextLogger,
    get_logger,
    setup_logging,
)


class TestGetLogger:
    """Tests for get_logger function."""

    def test_returns_context_logger(self) -> None:
        """get_logger returns a ContextLogger instance."""
        logger = get_logger("test.module")
        assert isinstance(logger, ContextLogger)

    def test_default_level_is_info(self) -> None:
        """Logger defaults to INFO level."""
        logger = get_logger("test.default_level")
        assert logger.logger.level == logging.INFO

    def test_custom_level(self) -> None:
        """Logger respects custom level argument."""
        logger = get_logger("test.custom_level", level=logging.DEBUG)
        assert logger.logger.level == logging.DEBUG

    def test_with_context(self) -> None:
        """Logger stores supplied context."""
        ctx = {"request_id": "abc123"}
        logger = get_logger("test.context", context=ctx)
        assert logger._context == ctx

    def test_with_log_file(self, tmp_path: Path) -> None:
        """Logger creates file handler when log_file is given."""
        log_file = str(tmp_path / "test.log")
        logger = get_logger("test.file_handler", log_file=log_file)
        handler_types = [type(h) for h in logger.logger.handlers]
        assert logging.FileHandler in handler_types

    def test_file_handler_added_after_console_only_call(self, tmp_path: Path) -> None:
        """Calling get_logger with log_file after a console-only call adds the file handler."""
        name = "test.file_after_console"
        logger1 = get_logger(name)
        assert len(logger1.logger.handlers) == 1

        log_file = str(tmp_path / "late.log")
        logger2 = get_logger(name, log_file=log_file)

        console_handlers = [
            h
            for h in logger2.logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        file_handlers = [h for h in logger2.logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(console_handlers) == 1
        assert len(file_handlers) == 1

    def test_no_duplicate_console_handler(self) -> None:
        """Repeated calls without log_file do not add duplicate console handlers."""
        name = "test.no_dup_console"
        get_logger(name)
        get_logger(name)

        underlying = logging.getLogger(name)
        console_handlers = [
            h
            for h in underlying.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert len(console_handlers) == 1

    def test_no_duplicate_file_handler_same_path(self, tmp_path: Path) -> None:
        """Repeated calls with the same log_file do not add duplicate file handlers."""
        name = "test.no_dup_file"
        log_file = str(tmp_path / "same.log")
        get_logger(name, log_file=log_file)
        get_logger(name, log_file=log_file)

        underlying = logging.getLogger(name)
        file_handlers = [h for h in underlying.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1

    def test_different_file_handlers_both_added(self, tmp_path: Path) -> None:
        """Calls with different log_file paths add separate file handlers."""
        name = "test.diff_files"
        file_a = str(tmp_path / "a.log")
        file_b = str(tmp_path / "b.log")
        get_logger(name, log_file=file_a)
        get_logger(name, log_file=file_b)

        underlying = logging.getLogger(name)
        file_handlers = [h for h in underlying.handlers if isinstance(h, logging.FileHandler)]
        base_filenames = {h.baseFilename for h in file_handlers}
        assert len(file_handlers) == 2
        assert str(Path(file_a).resolve()) in base_filenames
        assert str(Path(file_b).resolve()) in base_filenames

    def test_idempotent_full_call(self, tmp_path: Path) -> None:
        """Identical calls with both console and file produce no extra handlers."""
        name = "test.idempotent"
        log_file = str(tmp_path / "idem.log")
        get_logger(name, log_file=log_file)
        count_after_first = len(logging.getLogger(name).handlers)

        get_logger(name, log_file=log_file)
        count_after_second = len(logging.getLogger(name).handlers)

        assert count_after_first == count_after_second


class TestContextLogger:
    """Tests for ContextLogger adapter."""

    def test_bind_adds_context(self) -> None:
        """bind() returns new logger with merged context."""
        base = get_logger("test.bind", context={"a": 1})
        bound = base.bind(b=2)
        assert bound._context == {"a": 1, "b": 2}

    def test_bind_does_not_mutate_original(self) -> None:
        """bind() does not modify the original logger's context."""
        base = get_logger("test.bind_immutable", context={"a": 1})
        base.bind(b=2)
        assert "b" not in base._context

    def test_unbind_removes_key(self) -> None:
        """unbind() returns logger without the specified key."""
        base = get_logger("test.unbind", context={"a": 1, "b": 2})
        unbound = base.unbind("a")
        assert "a" not in unbound._context
        assert unbound._context["b"] == 2

    def test_init_copies_context_dict(self) -> None:
        """__init__ stores a copy, not the caller's original dict."""
        caller_dict: dict[str, object] = {"key": "value"}
        logger = ContextLogger(logging.getLogger("test.copy"), caller_dict)
        assert logger._context is not caller_dict
        assert logger._context == caller_dict

    def test_init_isolates_from_caller_mutation(self) -> None:
        """Mutating the caller's dict after construction does not affect the logger."""
        caller_dict: dict[str, object] = {"key": "original"}
        logger = ContextLogger(logging.getLogger("test.isolate"), caller_dict)
        caller_dict["key"] = "mutated"
        caller_dict["new_key"] = "surprise"
        assert logger._context["key"] == "original"
        assert "new_key" not in logger._context

    def test_get_logger_isolates_context(self) -> None:
        """get_logger() also isolates the context dict from caller mutation."""
        caller_dict: dict[str, object] = {"req": "abc"}
        logger = get_logger("test.get_logger_isolate", context=caller_dict)
        caller_dict["req"] = "changed"
        assert logger._context["req"] == "abc"

    def test_process_adds_extra(self) -> None:
        """process() merges context into kwargs['extra']."""
        logger = get_logger("test.process", context={"x": 42})
        _msg, kwargs = logger.process("hello", {})
        assert kwargs["extra"]["x"] == 42

    def test_bind_thread_safe(self) -> None:
        """Concurrent bind() calls do not corrupt context."""
        base = get_logger("test.thread_safe", context={"base": 0})
        results: list[dict[str, object]] = []

        def worker(val: int) -> None:
            bound = base.bind(val=val)
            results.append(dict(bound._context))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Original context must not be mutated
        assert "val" not in base._context
        # Each result must contain both base key and the thread-specific val
        for ctx in results:
            assert "base" in ctx
            assert "val" in ctx

    def test_process_and_bind_thread_safe(self) -> None:
        """Concurrent process() and bind() calls do not corrupt context."""
        base = get_logger("test.process_bind_safe", context={"base": 0})
        barrier = threading.Barrier(20)
        exceptions: list[Exception] = []
        process_results: list[dict[str, object]] = []
        results_lock = threading.Lock()

        def process_worker() -> None:
            try:
                barrier.wait()
                for _ in range(100):
                    _msg, kwargs = base.process("test", {})
                    with results_lock:
                        process_results.append(dict(kwargs.get("extra", {})))
            except Exception as exc:
                exceptions.append(exc)

        def bind_worker(val: int) -> None:
            try:
                barrier.wait()
                for _ in range(100):
                    base.bind(key=val)
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=process_worker) for _ in range(10)] + [
            threading.Thread(target=bind_worker, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Threads raised exceptions: {exceptions}"
        # Original context must not be mutated
        assert base._context == {"base": 0}
        # Every process() result must contain the base key
        for result in process_results:
            assert "base" in result

    def test_process_and_unbind_thread_safe(self) -> None:
        """Concurrent process() and unbind() calls do not corrupt context."""
        base = get_logger("test.process_unbind_safe", context={"base": 0, "removable": 1})
        barrier = threading.Barrier(20)
        exceptions: list[Exception] = []
        process_results: list[dict[str, object]] = []
        results_lock = threading.Lock()

        def process_worker() -> None:
            try:
                barrier.wait()
                for _ in range(100):
                    _msg, kwargs = base.process("test", {})
                    with results_lock:
                        process_results.append(dict(kwargs.get("extra", {})))
            except Exception as exc:
                exceptions.append(exc)

        def unbind_worker() -> None:
            try:
                barrier.wait()
                for _ in range(100):
                    base.unbind("removable")
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=process_worker) for _ in range(10)] + [
            threading.Thread(target=unbind_worker) for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not exceptions, f"Threads raised exceptions: {exceptions}"
        # Original context must not be mutated
        assert base._context == {"base": 0, "removable": 1}
        # Every process() result must contain the base key
        for result in process_results:
            assert "base" in result


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_runs_without_error(self) -> None:
        """setup_logging runs without raising."""
        setup_logging(level=logging.WARNING)

    def test_with_log_file(self, tmp_path: Path) -> None:
        """setup_logging creates log file handler."""
        log_file = str(tmp_path / "setup.log")
        setup_logging(log_file=log_file)
        root_logger = logging.getLogger()
        handler_files = [getattr(h, "baseFilename", None) for h in root_logger.handlers]
        assert log_file in handler_files

    def test_log_to_stderr(self) -> None:
        """setup_logging with log_to_stderr=True adds a stderr StreamHandler."""
        import sys

        root = logging.getLogger()
        # basicConfig is a no-op if handlers already exist; clear them first.
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_to_stderr=True)
            stderr_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            ]
            assert len(stderr_handlers) >= 1
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)
