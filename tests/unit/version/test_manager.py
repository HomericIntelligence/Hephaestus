#!/usr/bin/env python3

"""Tests for hephaestus.version.manager module."""

import pytest

from hephaestus.version.manager import VersionManager, parse_version


def test_parse_version_valid():
    """Test parsing valid version strings."""
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("0.0.1") == (0, 0, 1)
    assert parse_version("10.20.30") == (10, 20, 30)


def test_parse_version_invalid():
    """Test parsing invalid version strings."""
    with pytest.raises(ValueError):
        parse_version("1.2")  # Missing patch

    with pytest.raises(ValueError):
        parse_version("1.2.3.4")  # Too many components

    with pytest.raises(ValueError):
        parse_version("a.b.c")  # Non-numeric

    with pytest.raises(ValueError):
        parse_version("v1.2.3")  # Has 'v' prefix


def test_version_manager_update_version_file(tmp_path):
    """Test updating VERSION file."""
    version_file = tmp_path / "VERSION"

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
    )

    manager.update_version_file(version_file, "1.2.3", verbose=False)

    assert version_file.exists()
    assert version_file.read_text().strip() == "1.2.3"


def test_version_manager_update_init_file(tmp_path):
    """Test updating __init__.py file."""
    init_file = tmp_path / "__init__.py"
    init_file.write_text('__version__ = "0.1.0"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[],
        init_files=[init_file],
    )

    manager.update_init_file(init_file, "1.2.3", verbose=False)

    content = init_file.read_text()
    assert '__version__ = "1.2.3"' in content


def test_version_manager_update_all(tmp_path):
    """Test updating all version files."""
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    init_file.write_text('__version__ = "0.1.0"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
    )

    manager.update("1.2.3", verbose=False)

    assert version_file.read_text().strip() == "1.2.3"
    assert '__version__ = "1.2.3"' in init_file.read_text()


def test_version_manager_verify_consistent(tmp_path):
    """Test verification with consistent versions."""
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"

    version_file.write_text("1.2.3\n")
    init_file.write_text('__version__ = "1.2.3"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
    )

    assert manager.verify("1.2.3", verbose=False) is True


def test_version_manager_verify_inconsistent(tmp_path):
    """Test verification with inconsistent versions."""
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"

    version_file.write_text("1.2.3\n")
    init_file.write_text('__version__ = "0.1.0"\n')  # Different version

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
    )

    assert manager.verify("1.2.3", verbose=False) is False


def test_version_manager_verify_missing_file(tmp_path):
    """Test verification with missing files."""
    version_file = tmp_path / "VERSION"
    # Don't create the file

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
    )

    assert manager.verify("1.2.3", verbose=False) is False


def test_version_manager_auto_detect_init_files(tmp_path):
    """Test auto-detection of __init__.py files."""
    # Create package structure
    package_dir = tmp_path / "mypackage"
    package_dir.mkdir()
    init_file = package_dir / "__init__.py"
    init_file.write_text('__version__ = "0.1.0"\n')

    # Create manager with auto-detection
    manager = VersionManager(repo_root=tmp_path)

    # Should have found the init file
    assert len(manager.init_files) >= 1
    assert any(f.name == "__init__.py" for f in manager.init_files)


def test_version_manager_skip_test_directories(tmp_path):
    """Test that test directories are skipped in auto-detection."""
    # Create test directory with __init__.py
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    test_init = test_dir / "__init__.py"
    test_init.write_text('__version__ = "0.1.0"\n')

    # Create manager with auto-detection
    manager = VersionManager(repo_root=tmp_path)

    # Should NOT include tests/__init__.py
    for init_file in manager.init_files:
        assert "tests" not in init_file.parts


def test_version_manager_update_without_version_attribute(tmp_path):
    """Test updating init file without __version__ attribute."""
    init_file = tmp_path / "__init__.py"
    init_file.write_text("# No version here\n")

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[],
        init_files=[init_file],
    )

    # Should handle gracefully (no error)
    manager.update_init_file(init_file, "1.2.3", verbose=False)

    # File should remain unchanged
    assert "__version__" not in init_file.read_text()


def test_version_manager_auto_detect_nested_init_files(tmp_path):
    """Test auto-detection of nested __init__.py files (the */*/__init__.py glob)."""
    # Create a nested package structure: mypackage/subpkg/__init__.py
    subpkg_dir = tmp_path / "mypackage" / "subpkg"
    subpkg_dir.mkdir(parents=True)
    init_file = subpkg_dir / "__init__.py"
    init_file.write_text('__version__ = "0.1.0"\n')

    # Create manager with auto-detection (no explicit init_files)
    manager = VersionManager(repo_root=tmp_path)

    # Should find the nested __init__.py
    assert any(f == init_file for f in manager.init_files)
