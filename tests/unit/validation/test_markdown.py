#!/usr/bin/env python3
"""Smoke tests for `hephaestus.validation.markdown` CLI entry points.

Two entry points are exposed via [project.scripts] in pyproject.toml:

- `hephaestus-validate-links` → `hephaestus.validation.markdown:main`
- `hephaestus-check-readmes`   → `hephaestus.validation.markdown:check_readmes_main`

These tests exercise each `main()` end-to-end through `tmp_path` fixtures
so the validator's I/O surface is real (file reads, glob, link parsing)
without touching the real repo.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.validation.markdown import check_readmes_main, main


@pytest.fixture
def readme_dir(tmp_path: Path) -> Path:
    """Provide a tmp dir with one passing and one failing README."""
    good = tmp_path / "subproject_a"
    good.mkdir()
    (good / "README.md").write_text(
        "# Subproject A\n\n"
        "## Installation\n\n"
        "Run `pip install foo`.\n\n"
        "## Usage\n\n"
        "Run `foo --help`.\n",
        encoding="utf-8",
    )
    bad = tmp_path / "subproject_b"
    bad.mkdir()
    (bad / "README.md").write_text(
        "# Subproject B\n\nMissing required sections.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def linkable_dir(tmp_path: Path) -> Path:
    """Provide a tmp dir with two markdown files, one with a broken intra-repo link."""
    (tmp_path / "good.md").write_text(
        "# Good\n\nSee [intro](./intro.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "intro.md").write_text("# Intro\n", encoding="utf-8")
    (tmp_path / "bad.md").write_text(
        "# Bad\n\nSee [missing](./does_not_exist.md).\n",
        encoding="utf-8",
    )
    return tmp_path


class TestValidateLinksMain:
    """Smoke tests for `hephaestus.validation.markdown:main` (validate-links)."""

    def test_main_directory_not_found_returns_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Missing target dir exits 1 with ERROR on stderr."""
        bogus = tmp_path / "does_not_exist"
        monkeypatch.setattr(
            sys, "argv", ["hephaestus-validate-links", str(bogus), "--repo-root", str(tmp_path)]
        )
        rc = main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err
        assert str(bogus) in captured.err

    def test_main_valid_links_returns_0(
        self,
        linkable_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Directory with only valid links exits 0."""
        # Only the good.md file (skip bad.md by pointing at a subset).
        only_good = linkable_dir / "only_good"
        only_good.mkdir()
        (only_good / "good.md").write_text(
            "# Good\n\nSee [intro](../intro.md).\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-validate-links",
                str(only_good),
                "--repo-root",
                str(linkable_dir),
            ],
        )
        rc = main()
        assert rc == 0
        capsys.readouterr()  # drain output

    def test_main_json_output_emits_valid_json(
        self,
        linkable_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--json produces parseable JSON on stdout."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-validate-links",
                str(linkable_dir),
                "--repo-root",
                str(linkable_dir),
                "--json",
            ],
        )
        rc = main()
        out = capsys.readouterr().out.strip()
        # Whatever the validator finds, stdout must be parseable JSON.
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        assert "failed" in parsed or "files" in parsed or "results" in parsed
        assert rc in (0, 1)


class TestCheckReadmesMain:
    """Smoke tests for `hephaestus.validation.markdown:check_readmes_main`."""

    def test_check_readmes_directory_not_found_returns_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Missing target dir exits 1 with ERROR on stderr."""
        bogus = tmp_path / "missing"
        monkeypatch.setattr(sys, "argv", ["hephaestus-check-readmes", "--directory", str(bogus)])
        rc = check_readmes_main()
        assert rc == 1
        captured = capsys.readouterr()
        assert "ERROR" in captured.err

    def test_check_readmes_no_readmes_returns_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Empty dir with no READMEs is a pass."""
        monkeypatch.setattr(sys, "argv", ["hephaestus-check-readmes", "--directory", str(tmp_path)])
        rc = check_readmes_main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "No README.md" in captured.out

    def test_check_readmes_failing_readme_returns_1(
        self,
        readme_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A README missing required sections fails."""
        # Use a required section that subproject_b's README is missing on purpose.
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-check-readmes",
                "--directory",
                str(readme_dir),
                "--required-section",
                "Installation",
                "--required-section",
                "Usage",
            ],
        )
        rc = check_readmes_main()
        assert rc == 1, "subproject_b is missing required sections; must fail"

    def test_check_readmes_passing_readme_returns_0(
        self,
        readme_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When only the passing README is scanned, exit 0."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-check-readmes",
                "--directory",
                str(readme_dir / "subproject_a"),
                "--required-section",
                "Installation",
                "--required-section",
                "Usage",
            ],
        )
        rc = check_readmes_main()
        assert rc == 0

    def test_check_readmes_json_output_is_valid(
        self,
        readme_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--json emits a JSON array."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-check-readmes",
                "--directory",
                str(readme_dir),
                "--json",
            ],
        )
        check_readmes_main()
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert all("file" in r and "passed" in r for r in parsed)

    def test_check_readmes_json_with_no_readmes_emits_object(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--json + empty dir emits the documented `{directory, results: []}` envelope."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-check-readmes",
                "--directory",
                str(tmp_path),
                "--json",
            ],
        )
        rc = check_readmes_main()
        assert rc == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed == {"directory": str(tmp_path), "results": []}

    def test_check_readmes_verbose_logs_each_file(
        self,
        readme_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--verbose prints `[PASS]`/`[FAIL]` per file."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hephaestus-check-readmes",
                "--directory",
                str(readme_dir),
                "--verbose",
                "--required-section",
                "Installation",
                "--required-section",
                "Usage",
            ],
        )
        check_readmes_main()
        captured = capsys.readouterr()
        assert "[PASS]" in captured.out or "[FAIL]" in captured.out


class TestPatchedSubprocessSeams:
    """Verify the module's argparse seam, not full validator semantics."""

    def test_main_module_importable_via_entry_point(self) -> None:
        """The module is importable as a callable from its console_script target."""
        with patch("hephaestus.validation.markdown.validate_all_links") as mocked:
            mocked.return_value = {"failed": [], "files": []}
            from hephaestus.validation import markdown as mod

            assert callable(mod.main)
            assert callable(mod.check_readmes_main)
