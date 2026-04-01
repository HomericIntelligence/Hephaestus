"""Tests for scripts/check_version_single_source.py."""

import sys
from pathlib import Path

import pytest

# Add scripts directory to path so we can import the module directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_version_single_source import (
    check_pixi_no_version,
    check_pyproject_has_version,
)


class TestCheckPyprojectHasVersion:
    """Tests for check_pyproject_has_version()."""

    def test_returns_true_when_version_present(self, tmp_path: Path) -> None:
        """Returns True when pyproject.toml has [project].version."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "mypkg"\nversion = "1.2.3"\n')
        assert check_pyproject_has_version(tmp_path) is True

    def test_returns_false_when_pyproject_missing(self, tmp_path: Path) -> None:
        """Returns False when pyproject.toml does not exist."""
        assert check_pyproject_has_version(tmp_path) is False

    def test_returns_false_when_no_version_in_project(self, tmp_path: Path) -> None:
        """Returns False when [project] section has no version key."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "mypkg"\ndescription = "no version"\n')
        assert check_pyproject_has_version(tmp_path) is False

    def test_returns_false_when_version_only_in_other_section(self, tmp_path: Path) -> None:
        """Returns False when version exists only in a non-[project] section."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.poetry]\nversion = "1.0.0"\n\n[project]\nname = "mypkg"\n')
        assert check_pyproject_has_version(tmp_path) is False

    def test_prints_ok_on_success(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Prints OK message with version when found."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "2.0.0"\n')
        check_pyproject_has_version(tmp_path)
        captured = capsys.readouterr()
        assert "2.0.0" in captured.out

    def test_prints_error_on_missing_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Prints ERROR message when pyproject.toml is missing."""
        check_pyproject_has_version(tmp_path)
        captured = capsys.readouterr()
        assert "ERROR" in captured.out


class TestCheckPixiNoVersion:
    """Tests for check_pixi_no_version()."""

    def test_returns_true_when_pixi_absent(self, tmp_path: Path) -> None:
        """Returns True when pixi.toml does not exist."""
        assert check_pixi_no_version(tmp_path) is True

    def test_returns_true_when_no_workspace_section(self, tmp_path: Path) -> None:
        """Returns True when pixi.toml has no [workspace] section."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[dependencies]\npython = ">=3.10"\n')
        assert check_pixi_no_version(tmp_path) is True

    def test_returns_true_when_workspace_has_no_version(self, tmp_path: Path) -> None:
        """Returns True when [workspace] exists but has no version field."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\nchannels = ["conda-forge"]\n')
        assert check_pixi_no_version(tmp_path) is True

    def test_returns_false_when_workspace_has_version(self, tmp_path: Path) -> None:
        """Returns False when pixi.toml [workspace] contains a version field."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\nversion = "1.0.0"\n')
        assert check_pixi_no_version(tmp_path) is False

    def test_returns_true_when_version_in_other_section(self, tmp_path: Path) -> None:
        """Returns True when version appears in a section other than [workspace]."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[workspace]\nname = "myproject"\n\n'
            '[feature.dev.pypi-dependencies]\nmypkg = {version = ">=1.0"}\n'
        )
        assert check_pixi_no_version(tmp_path) is True

    def test_prints_error_on_version_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Prints ERROR message when workspace version is found."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nversion = "0.1.0"\n')
        check_pixi_no_version(tmp_path)
        captured = capsys.readouterr()
        assert "ERROR" in captured.out

    def test_prints_ok_when_no_version(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Prints OK message when pixi.toml has no workspace version."""
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\n')
        check_pixi_no_version(tmp_path)
        captured = capsys.readouterr()
        assert "OK" in captured.out


class TestMain:
    """Tests for main()."""

    def test_returns_0_when_all_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns 0 when pyproject.toml has version and pixi.toml has none."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.0.0"\n')
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nname = "myproject"\n')

        monkeypatch.chdir(tmp_path)
        # Patch get_repo_root in the module's namespace
        import check_version_single_source as mod

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 0

    def test_returns_1_when_pyproject_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 1 when pyproject.toml does not exist."""
        import check_version_single_source as mod

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 1

    def test_returns_1_when_pixi_has_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 1 when pixi.toml [workspace] has a version field."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.0.0"\n')
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[workspace]\nversion = "1.0.0"\n')

        import check_version_single_source as mod

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 1

    def test_returns_0_when_pixi_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns 0 when pixi.toml does not exist (only pyproject.toml)."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nversion = "1.0.0"\n')

        import check_version_single_source as mod

        monkeypatch.setattr(mod, "get_repo_root", lambda: tmp_path)
        assert mod.main() == 0
