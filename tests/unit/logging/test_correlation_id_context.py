#!/usr/bin/env python3
"""Unit tests for correlation ID context variable functionality."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest

from hephaestus.logging.utils import (
    _correlation_id_var,
    correlation_id_scope,
    get_current_correlation_id,
    set_correlation_id,
)


@pytest.fixture(autouse=True)
def _reset_correlation_id() -> Generator[None, None, None]:
    """Reset correlation ID to None before each test."""
    token = _correlation_id_var.set(None)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)


class TestSetAndGetCorrelationId:
    """Test set_correlation_id() and get_current_correlation_id()."""

    def test_get_correlation_id_none_by_default(self) -> None:
        """get_current_correlation_id() returns None when not set."""
        assert get_current_correlation_id() is None

    def test_set_correlation_id(self) -> None:
        """set_correlation_id() sets the correlation ID."""
        token = set_correlation_id("test-id-123")
        try:
            assert get_current_correlation_id() == "test-id-123"
        finally:
            _correlation_id_var.reset(token)
        assert get_current_correlation_id() is None

    def test_set_correlation_id_token_reset(self) -> None:
        """Token.reset() restores the previous value."""
        # Initial value: None
        assert get_current_correlation_id() is None

        # Set first ID
        token1 = set_correlation_id("id-1")
        assert get_current_correlation_id() == "id-1"

        # Set second ID (nested)
        token2 = set_correlation_id("id-2")
        assert get_current_correlation_id() == "id-2"

        # Reset second ID (restore id-1)
        _correlation_id_var.reset(token2)
        assert get_current_correlation_id() == "id-1"

        # Reset first ID (restore None)
        _correlation_id_var.reset(token1)
        assert get_current_correlation_id() is None

    def test_set_multiple_times(self) -> None:
        """Setting correlation ID multiple times (without nesting) overwrites."""
        token1 = set_correlation_id("id-1")
        assert get_current_correlation_id() == "id-1"

        token2 = set_correlation_id("id-2")
        assert get_current_correlation_id() == "id-2"

        # Reset token2
        _correlation_id_var.reset(token2)
        # After resetting token2, we're back to id-1 (the context before token2)
        assert get_current_correlation_id() == "id-1"

        _correlation_id_var.reset(token1)
        assert get_current_correlation_id() is None


class TestCorrelationIdScope:
    """Test correlation_id_scope() context manager."""

    def test_scope_sets_and_restores(self) -> None:
        """correlation_id_scope() sets ID on entry, restores on exit."""
        assert get_current_correlation_id() is None

        with correlation_id_scope("scope-id-123"):
            assert get_current_correlation_id() == "scope-id-123"

        assert get_current_correlation_id() is None

    def test_scope_nested(self) -> None:
        """Nested correlation_id_scope() contexts work correctly."""
        assert get_current_correlation_id() is None

        with correlation_id_scope("outer"):
            assert get_current_correlation_id() == "outer"

            with correlation_id_scope("inner"):
                assert get_current_correlation_id() == "inner"

            assert get_current_correlation_id() == "outer"

        assert get_current_correlation_id() is None

    def test_scope_exception_cleanup(self) -> None:
        """correlation_id_scope() cleans up even if an exception is raised."""
        assert get_current_correlation_id() is None

        with pytest.raises(ValueError):
            with correlation_id_scope("error-scope"):
                assert get_current_correlation_id() == "error-scope"
                raise ValueError("test error")

        # Even after exception, context is restored
        assert get_current_correlation_id() is None

    def test_scope_preserves_existing(self) -> None:
        """correlation_id_scope() preserves ID from outer scope."""
        with correlation_id_scope("outer"):
            assert get_current_correlation_id() == "outer"

            with correlation_id_scope("inner"):
                assert get_current_correlation_id() == "inner"

            assert get_current_correlation_id() == "outer"


class TestCorrelationIdThreadIsolation:
    """Test that correlation IDs are thread-isolated via contextvars."""

    def test_different_threads_have_different_values(self) -> None:
        """Each thread has its own correlation ID value."""
        results: dict[int, str | None] = {}

        def get_id_in_thread(thread_id: int) -> None:
            # Set a thread-specific ID
            token = set_correlation_id(f"thread-{thread_id}")
            try:
                # Sleep to ensure threads overlap
                import time

                time.sleep(0.01)
                results[thread_id] = get_current_correlation_id()
            finally:
                _correlation_id_var.reset(token)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(get_id_in_thread, i) for i in range(3)]
            for future in futures:
                future.result()

        # Each thread saw its own value
        assert results == {0: "thread-0", 1: "thread-1", 2: "thread-2"}

    def test_outer_thread_unaffected_by_inner_threads(self) -> None:
        """Setting correlation ID in a thread doesn't affect the outer thread."""
        outer_before = get_current_correlation_id()
        outer_token = set_correlation_id("outer-id")

        try:

            def set_in_thread() -> str | None:
                inner_token = set_correlation_id("inner-id")
                try:
                    return get_current_correlation_id()
                finally:
                    _correlation_id_var.reset(inner_token)

            with ThreadPoolExecutor(max_workers=1) as executor:
                inner_result = executor.submit(set_in_thread).result()

            # Inner thread had its own value
            assert inner_result == "inner-id"

            # Outer thread is unaffected
            assert get_current_correlation_id() == "outer-id"
        finally:
            _correlation_id_var.reset(outer_token)
            assert get_current_correlation_id() == outer_before
