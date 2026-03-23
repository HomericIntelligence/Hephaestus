"""Tests for hephaestus.validation.test_structure."""

from pathlib import Path

import pytest

from hephaestus.validation.test_structure import (
    check_no_loose_test_files,
    check_test_directory_mirrors,
    check_test_structure,
)


def _make_package(root: Path, name: str) -> Path:
    """Create a minimal Python package directory."""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").touch()
    return pkg


class TestCheckTestDirectoryMirrors:
    """Tests for check_test_directory_mirrors()."""

    def test_all_mirrored(self, tmp_path: Path) -> None:
        """Returns True when all source subpackages have test dirs."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        for name in ["utils", "config", "io"]:
            _make_package(src, name)
            (tests / name).mkdir(parents=True)
        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored is True
        assert missing == set()

    def test_missing_test_dir(self, tmp_path: Path) -> None:
        """Returns False with missing dirs listed."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        _make_package(src, "config")
        (tests / "utils").mkdir(parents=True)
        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored is False
        assert missing == {"config"}

    def test_empty_source(self, tmp_path: Path) -> None:
        """Empty source package passes."""
        src = tmp_path / "mypackage"
        src.mkdir()
        tests = tmp_path / "tests" / "unit"
        tests.mkdir(parents=True)
        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored is True

    def test_ignores_hidden_dirs(self, tmp_path: Path) -> None:
        """Directories starting with . or _ are ignored."""
        src = tmp_path / "mypackage"
        tests = tmp_path / "tests" / "unit"
        _make_package(src, "utils")
        (src / "__pycache__").mkdir()
        (src / ".hidden").mkdir()
        (tests / "utils").mkdir(parents=True)
        mirrored, missing = check_test_directory_mirrors(src, tests)
        assert mirrored is True


class TestCheckNoLooseTestFiles:
    """Tests for check_no_loose_test_files()."""

    def test_clean_structure(self, tmp_path: Path) -> None:
        """No loose test files returns True."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "__init__.py").touch()
        (unit_root / "conftest.py").touch()
        sub = unit_root / "utils"
        sub.mkdir()
        (sub / "test_helpers.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is True
        assert violations == []

    def test_loose_test_file_detected(self, tmp_path: Path) -> None:
        """Loose test_*.py at root is flagged."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "test_bad.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is False
        assert len(violations) == 1
        assert violations[0].name == "test_bad.py"

    def test_allowed_files_not_flagged(self, tmp_path: Path) -> None:
        """__init__.py and conftest.py are allowed at root."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "__init__.py").touch()
        (unit_root / "conftest.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is True

    def test_missing_directory(self, tmp_path: Path) -> None:
        """Missing directory returns True (no violations)."""
        no_loose, violations = check_no_loose_test_files(tmp_path / "nonexistent")
        assert no_loose is True

    def test_multiple_loose_files(self, tmp_path: Path) -> None:
        """Multiple loose files all detected."""
        unit_root = tmp_path / "tests" / "unit"
        unit_root.mkdir(parents=True)
        (unit_root / "test_a.py").touch()
        (unit_root / "test_b.py").touch()
        no_loose, violations = check_no_loose_test_files(unit_root)
        assert no_loose is False
        assert len(violations) == 2


class TestCheckTestStructure:
    """Tests for check_test_structure()."""

    def test_passing_structure(self, tmp_path: Path) -> None:
        """Correctly structured project passes both checks."""
        # Create source package
        src = tmp_path / "mypackage"
        _make_package(src, "utils")
        _make_package(src, "config")
        (src / "__init__.py").touch()

        # Create matching test structure
        test_root = tmp_path / "tests" / "unit"
        utils_tests = test_root / "utils"
        utils_tests.mkdir(parents=True)
        (utils_tests / "test_helpers.py").touch()
        config_tests = test_root / "config"
        config_tests.mkdir()
        (config_tests / "test_config.py").touch()

        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is True

    def test_missing_src_root(self, tmp_path: Path) -> None:
        """Missing source root returns False."""
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        passed = check_test_structure(tmp_path, src_package="nonexistent")
        assert passed is False

    def test_missing_test_root(self, tmp_path: Path) -> None:
        """Missing test root returns False."""
        src = tmp_path / "mypackage"
        src.mkdir()
        (src / "__init__.py").touch()
        passed = check_test_structure(tmp_path, src_package="mypackage")
        assert passed is False
