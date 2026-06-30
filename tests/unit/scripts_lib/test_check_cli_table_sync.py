"""Tests for hephaestus.scripts_lib.check_cli_table_sync.

The script verifies two invariants:

1. Every command listed in ``pyproject.toml [project.scripts]`` is mentioned
   somewhere in ``README.md`` (the original purpose of the script).
2. The human-readable prose counts in ``README.md`` and ``docs/index.md``
   agree with the actual ``[project.scripts]`` length.  This second check was
   added by #857 after the README, the docs, and the table disagreed (42 vs.
   37+ vs. 44).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.scripts_lib.check_cli_table_sync import check_prose_counts


class TestCheckProseCounts:
    """Tests for ``check_prose_counts``."""

    def _make_repo(
        self,
        tmp_path: Path,
        *,
        readme_count: str | int | None,
        docs_count: str | int | None,
    ) -> Path:
        """Build a scratch repo with the requested prose counts.

        Passing ``None`` for either ``readme_count`` or ``docs_count`` omits
        the relevant prose sentence entirely so the checker can be exercised
        against missing-sentence cases.
        """
        readme = tmp_path / "README.md"
        if readme_count is None:
            readme.write_text("# Project\n\nNo CLI mention here.\n")
        else:
            readme.write_text(
                f"# Project\n\n{readme_count} console scripts are installed when "
                "you install the package.\n"
            )

        docs = tmp_path / "docs"
        docs.mkdir()
        docs_index = docs / "index.md"
        if docs_count is None:
            docs_index.write_text("# Docs\n\nNo CLI mention here.\n")
        else:
            docs_index.write_text(
                f"# Docs\n\nFull function signatures for all {docs_count} CLI entry points.\n"
            )

        return tmp_path

    def test_returns_true_when_both_counts_match(self, tmp_path: Path) -> None:
        """Matching prose counts in README and docs/index return ok=True."""
        repo = self._make_repo(tmp_path, readme_count=44, docs_count=44)
        ok, mismatches = check_prose_counts(repo, expected_count=44)
        assert ok is True
        assert mismatches == []

    def test_returns_false_on_readme_mismatch(self, tmp_path: Path) -> None:
        """A wrong README count produces a clear mismatch message."""
        repo = self._make_repo(tmp_path, readme_count=42, docs_count=44)
        ok, mismatches = check_prose_counts(repo, expected_count=44)
        assert ok is False
        assert any("README.md" in m and "42" in m and "44" in m for m in mismatches), mismatches

    def test_returns_false_on_docs_index_mismatch(self, tmp_path: Path) -> None:
        """A wrong docs/index.md count produces a clear mismatch message."""
        repo = self._make_repo(tmp_path, readme_count=44, docs_count=37)
        ok, mismatches = check_prose_counts(repo, expected_count=44)
        assert ok is False
        assert any("docs/index.md" in m and "37" in m and "44" in m for m in mismatches), mismatches

    def test_legacy_plus_suffix_still_parsed(self, tmp_path: Path) -> None:
        """The docs/index pattern strips a trailing ``+`` like '37+' before comparing.

        The original drift this guard exists to catch was ``37+ CLI entry
        points`` vs. the real count of 44, so the ``+`` must still be parsed
        as a mismatch rather than silently skipped.
        """
        repo = self._make_repo(tmp_path, readme_count=44, docs_count="37+")
        ok, mismatches = check_prose_counts(repo, expected_count=44)
        assert ok is False
        assert any("docs/index.md" in m and "37" in m for m in mismatches), mismatches

    def test_missing_readme_prose_is_a_mismatch(self, tmp_path: Path) -> None:
        """If the README prose sentence is missing, that is treated as a mismatch.

        Silently passing when the wording disappears would defeat the entire
        purpose of the guard rail.
        """
        repo = self._make_repo(tmp_path, readme_count=None, docs_count=44)
        ok, mismatches = check_prose_counts(repo, expected_count=44)
        assert ok is False
        assert any("README.md" in m and "missing prose" in m for m in mismatches), mismatches

    def test_missing_docs_index_prose_is_a_mismatch(self, tmp_path: Path) -> None:
        """If the docs/index.md prose sentence is missing, that is a mismatch."""
        repo = self._make_repo(tmp_path, readme_count=44, docs_count=None)
        ok, mismatches = check_prose_counts(repo, expected_count=44)
        assert ok is False
        assert any("docs/index.md" in m and "missing prose" in m for m in mismatches), mismatches

    def test_missing_readme_file_is_a_mismatch(self, tmp_path: Path) -> None:
        """If README.md does not exist at all, that is reported as a mismatch."""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "index.md").write_text("Full function signatures for all 44 CLI entry points.\n")
        ok, mismatches = check_prose_counts(tmp_path, expected_count=44)
        assert ok is False
        assert any("README.md not found" in m for m in mismatches), mismatches

    def test_missing_docs_index_file_is_a_mismatch(self, tmp_path: Path) -> None:
        """If docs/index.md does not exist at all, that is reported."""
        (tmp_path / "README.md").write_text("44 console scripts are installed.\n")
        ok, mismatches = check_prose_counts(tmp_path, expected_count=44)
        assert ok is False
        assert any("docs/index.md not found" in m for m in mismatches), mismatches


class TestMain:
    """End-to-end tests for ``main`` against the real repository."""

    def test_returns_0_against_real_repo(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The checker must PASS on the actual repository configuration.

        This catches drift the moment a contributor lands a new console script
        without bumping the README/docs prose.
        """
        from hephaestus.scripts_lib import check_cli_table_sync as mod

        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "OK" in out
