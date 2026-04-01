#!/usr/bin/env python3
"""Tests for logging utilities."""

import io
import json
import logging
import os
import sys
import threading
from io import StringIO
from pathlib import Path

from hephaestus.logging.formatters import JsonFormatter
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

    def test_concurrent_no_duplicate_handlers(self) -> None:
        """Concurrent get_logger calls for the same name must not add duplicate handlers."""
        logger_name = "test.concurrent_handler_safety"
        # Ensure a fresh logger with no handlers
        underlying = logging.getLogger(logger_name)
        underlying.handlers.clear()

        num_threads = 20
        barrier = threading.Barrier(num_threads)
        results: list[ContextLogger] = []
        results_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            ctx_logger = get_logger(logger_name)
            with results_lock:
                results.append(ctx_logger)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads got a logger, but the underlying logger must have exactly 1 handler
        assert len(results) == num_threads
        assert len(underlying.handlers) == 1
        assert isinstance(underlying.handlers[0], logging.StreamHandler)

    def test_sequential_no_duplicate_handlers(self) -> None:
        """Calling get_logger twice for the same name does not duplicate handlers."""
        logger_name = "test.sequential_no_dup"
        underlying = logging.getLogger(logger_name)
        underlying.handlers.clear()

        get_logger(logger_name)
        get_logger(logger_name)

        assert len(underlying.handlers) == 1

    def test_propagate_false_when_handlers_added(self) -> None:
        """get_logger sets propagate=False to prevent duplicate output."""
        logger = get_logger("test.propagate_false")
        assert logger.logger.propagate is False

    def test_no_duplicate_output_with_root_handlers(self) -> None:
        """Messages appear once even when root logger has handlers."""
        # Set up root logger with a capturing handler
        root = logging.getLogger()
        capture_stream = io.StringIO()
        root_handler = logging.StreamHandler(capture_stream)
        root_handler.setFormatter(logging.Formatter("ROOT: %(message)s"))
        root.addHandler(root_handler)
        root.setLevel(logging.DEBUG)

        try:
            child = get_logger("test.no_dup")
            # Replace the child's console handler stream for capturing
            child_capture = io.StringIO()
            for h in child.logger.handlers:
                if isinstance(h, logging.StreamHandler):
                    h.stream = child_capture

            child.info("test message")

            # Child should have the message
            assert "test message" in child_capture.getvalue()
            # Root should NOT have the message (propagation disabled)
            assert "test message" not in capture_stream.getvalue()
        finally:
            root.removeHandler(root_handler)

    def test_propagate_unchanged_on_subsequent_calls(self) -> None:
        """Second get_logger() call with same name preserves propagate=False."""
        logger1 = get_logger("test.subsequent_propagate")
        assert logger1.logger.propagate is False
        logger2 = get_logger("test.subsequent_propagate")
        assert logger2.logger.propagate is False

    def test_json_format_uses_json_formatter(self) -> None:
        """get_logger with json_format=True uses JsonFormatter on handlers."""
        logger = get_logger("test.json_fmt", json_format=True)
        for handler in logger.logger.handlers:
            assert isinstance(handler.formatter, JsonFormatter)

    def test_json_format_output_is_json(self) -> None:
        """get_logger with json_format=True produces valid JSON output."""
        logger = get_logger("test.json_output", json_format=True)
        # Capture output from the handler
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logger.logger.handlers[0].formatter)
        logger.logger.addHandler(handler)
        try:
            logger.info("hello json")
            output = stream.getvalue().strip()
            parsed = json.loads(output)
            assert parsed["message"] == "hello json"
            assert parsed["level"] == "INFO"
        finally:
            logger.logger.removeHandler(handler)

    def test_json_format_with_context(self) -> None:
        """Bound context fields appear in JSON output."""
        logger = get_logger("test.json_ctx", json_format=True)
        bound = logger.bind(request_id="req-42", service="odyssey")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logger.logger.handlers[0].formatter)
        bound.logger.addHandler(handler)
        try:
            bound.info("ctx test")
            output = stream.getvalue().strip()
            parsed = json.loads(output)
            assert parsed["request_id"] == "req-42"
            assert parsed["service"] == "odyssey"
        finally:
            bound.logger.removeHandler(handler)


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
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(level=logging.WARNING)
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_with_log_file(self, tmp_path: Path) -> None:
        """setup_logging creates log file handler."""
        log_file = str(tmp_path / "setup.log")
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_file=log_file)
            handler_files = [getattr(h, "baseFilename", None) for h in root.handlers]
            assert os.path.abspath(log_file) in handler_files
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_log_to_stderr(self) -> None:
        """setup_logging with log_to_stderr=True adds a stderr StreamHandler."""
        root = logging.getLogger()
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

    def test_no_duplicate_file_handler(self, tmp_path: Path) -> None:
        """Calling setup_logging with same log_file twice adds only one FileHandler."""
        log_file = str(tmp_path / "dup.log")
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_file=log_file)
            setup_logging(log_file=log_file)
            file_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.FileHandler)
                and h.baseFilename == os.path.abspath(log_file)
            ]
            assert len(file_handlers) == 1
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_no_duplicate_stdout_handler(self) -> None:
        """Calling setup_logging twice adds only one stdout StreamHandler."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging()
            setup_logging()
            stdout_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and h.stream is sys.stdout
            ]
            assert len(stdout_handlers) == 1
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_no_duplicate_stderr_handler(self) -> None:
        """Calling setup_logging with log_to_stderr=True twice adds only one stderr handler."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_to_stderr=True)
            setup_logging(log_to_stderr=True)
            stderr_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and h.stream is sys.stderr
            ]
            assert len(stderr_handlers) == 1
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_different_log_files_add_separate_handlers(self, tmp_path: Path) -> None:
        """Different log file paths correctly add separate FileHandlers."""
        log_a = str(tmp_path / "a.log")
        log_b = str(tmp_path / "b.log")
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_file=log_a)
            setup_logging(log_file=log_b)
            file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
            assert len(file_handlers) == 2
            basenames = {h.baseFilename for h in file_handlers}
            assert os.path.abspath(log_a) in basenames
            assert os.path.abspath(log_b) in basenames
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_json_format_configures_json_formatter(self) -> None:
        """setup_logging with json_format=True uses JsonFormatter on all handlers."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(json_format=True)
            assert len(root.handlers) >= 1
            for handler in root.handlers:
                assert isinstance(handler.formatter, JsonFormatter)
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_json_format_with_log_file(self, tmp_path: Path) -> None:
        """setup_logging with json_format=True also applies to file handler."""
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            log_file = str(tmp_path / "json_setup.log")
            setup_logging(json_format=True, log_file=log_file)
            file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
            assert len(file_handlers) >= 1
            assert isinstance(file_handlers[0].formatter, JsonFormatter)
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)

    def test_adding_stderr_on_second_call(self) -> None:
        """Calling setup_logging() twice with different parameters correctly adds new handler.

        First call without stderr, second call with log_to_stderr=True.
        The stderr handler should be added without replacing the stdout handler.
        """
        root = logging.getLogger()
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging()
            stdout_count = sum(
                1
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and getattr(h, "stream", None) is sys.stdout
            )
            assert stdout_count == 1

            setup_logging(log_to_stderr=True)
            stderr_count = sum(
                1
                for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and getattr(h, "stream", None) is sys.stderr
            )
            assert stderr_count == 1, "stderr handler should be added on second call"
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)
