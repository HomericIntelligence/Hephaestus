#!/usr/bin/env python3
"""Integration test for GH_TRACE_ID environment variable propagation."""

import os
from pathlib import Path

import pytest

from hephaestus.logging.utils import correlation_id_scope, get_current_correlation_id
from hephaestus.utils.helpers import run_subprocess

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _fake_binary_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Add a temporary directory with fake 'gh' and 'git' binaries to PATH.

    These fake binaries output GH_TRACE_ID if set, else NO_GH_TRACE_ID.
    """
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()

    # Create a fake 'gh' script that outputs environment
    fake_gh = fake_bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "(env | grep -q '^GH_TRACE_ID=' && env | grep '^GH_TRACE_ID=') || echo 'NO_GH_TRACE_ID'\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    # Create a fake 'git' script that outputs environment
    fake_git = fake_bin_dir / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "(env | grep -q '^GH_TRACE_ID=' && env | grep '^GH_TRACE_ID=') || echo 'NO_GH_TRACE_ID'\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    # Prepend the fake bin directory to PATH so our fake binaries are found first
    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(fake_bin_dir) + ":" + original_path)


class TestGhTraceIdPropagation:
    """Test that GH_TRACE_ID is propagated to subprocesses via correlation_id_scope."""

    def test_correlation_id_not_set_by_default(self) -> None:
        """Verify that get_current_correlation_id() returns None by default."""
        assert get_current_correlation_id() is None

    def test_gh_trace_id_injected_via_run_subprocess(self) -> None:
        """GH_TRACE_ID should be injected into the subprocess environment via run_subprocess."""
        # Outside scope: no GH_TRACE_ID should be set
        result = run_subprocess(
            ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
            check=False,
        )
        assert "NO_GH_TRACE_ID" in result.stdout

        # Inside scope: GH_TRACE_ID should be present
        with correlation_id_scope("test-trace-id-123"):
            result = run_subprocess(
                ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
                check=False,
            )
            assert "GH_TRACE_ID=test-trace-id-123" in result.stdout

    def test_correlation_id_preserved_across_scopes(self) -> None:
        """Nested correlation_id_scope should propagate the innermost ID."""
        with correlation_id_scope("outer-id"):
            result_outer = run_subprocess(
                ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
                check=False,
            )
            assert "GH_TRACE_ID=outer-id" in result_outer.stdout

            with correlation_id_scope("inner-id"):
                result_inner = run_subprocess(
                    ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
                    check=False,
                )
                assert "GH_TRACE_ID=inner-id" in result_inner.stdout

            # After exiting inner scope, outer ID should be restored
            result_back = run_subprocess(
                ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
                check=False,
            )
            assert "GH_TRACE_ID=outer-id" in result_back.stdout

    def test_correlation_id_cleanup_after_exception(self) -> None:
        """Correlation ID should be cleaned up even if an exception occurs in the scope."""
        try:
            with correlation_id_scope("error-scope-id"):
                result = run_subprocess(
                    ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
                    check=False,
                )
                assert "GH_TRACE_ID=error-scope-id" in result.stdout
                raise ValueError("Test exception")
        except ValueError:
            pass

        # After exception: correlation ID should be cleared
        assert get_current_correlation_id() is None
        result = run_subprocess(
            ["sh", "-c", "env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'"],
            check=False,
        )
        assert "NO_GH_TRACE_ID" in result.stdout

    def test_custom_env_dict_preserved(self) -> None:
        """Verify GH_TRACE_ID is injected into a custom env dict passed to run_subprocess."""
        custom_env = {"CUSTOM_VAR": "custom_value"}

        with correlation_id_scope("custom-trace-id"):
            result = run_subprocess(
                [
                    "sh",
                    "-c",
                    "echo CUSTOM_VAR=$CUSTOM_VAR; env | grep GH_TRACE_ID || echo 'NO_GH_TRACE_ID'",
                ],
                env=custom_env,
                check=False,
            )
            # Custom var should still be present
            assert "CUSTOM_VAR=custom_value" in result.stdout
            # GH_TRACE_ID should be injected
            assert "GH_TRACE_ID=custom-trace-id" in result.stdout
