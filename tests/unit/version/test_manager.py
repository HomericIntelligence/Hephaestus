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


def test_version_manager_verify_missing_init_file_is_optional(tmp_path):
    """verify() treats a missing __init__.py as optional (returns True, logs warning)."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")
    missing_init = tmp_path / "missing_pkg" / "__init__.py"

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[missing_init],
    )

    # Missing __init__.py is optional — version file check passes so overall True
    assert manager.verify("1.2.3", verbose=False) is True


def test_version_manager_verify_init_without_version_attribute(tmp_path):
    """verify() returns False when __init__.py lacks __version__ attribute."""
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"

    version_file.write_text("1.2.3\n")
    init_file.write_text("# no __version__ here\n")

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
    )

    assert manager.verify("1.2.3", verbose=False) is False


def test_version_manager_verify_verbose_consistent(tmp_path):
    """verify(verbose=True) returns True for consistent versions without raising."""
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    version_file.write_text("2.0.0\n")
    init_file.write_text('__version__ = "2.0.0"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
    )

    assert manager.verify("2.0.0", verbose=True) is True


def test_version_manager_verify_verbose_inconsistent(tmp_path):
    """verify(verbose=True) returns False for inconsistent versions without raising."""
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    version_file.write_text("1.0.0\n")
    init_file.write_text('__version__ = "2.0.0"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
    )

    assert manager.verify("1.0.0", verbose=True) is False


def test_version_manager_verify_multiple_init_files_mixed(tmp_path):
    """verify() returns False when at least one __init__.py has the wrong version."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.5.0\n")
    good_init = tmp_path / "pkg_a" / "__init__.py"
    bad_init = tmp_path / "pkg_b" / "__init__.py"
    good_init.parent.mkdir()
    bad_init.parent.mkdir()
    good_init.write_text('__version__ = "1.5.0"\n')
    bad_init.write_text('__version__ = "0.0.1"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[good_init, bad_init],
    )

    assert manager.verify("1.5.0", verbose=False) is False


def test_version_manager_update_pyproject_file(tmp_path):
    """update_pyproject_file() writes the new version into pyproject.toml [project].version."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "mypkg"\nversion = "0.1.0"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[],
        init_files=[],
        pyproject_file=pyproject,
    )

    manager.update_pyproject_file(pyproject, "2.3.4", verbose=False)

    content = pyproject.read_text()
    assert 'version = "2.3.4"' in content
    assert 'version = "0.1.0"' not in content


def test_version_manager_update_pyproject_no_version_section(tmp_path):
    """update_pyproject_file() leaves file unchanged when no [project].version found."""
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.mypy]\npython_version = "3.10"\n'
    pyproject.write_text(original)

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[],
        init_files=[],
        pyproject_file=pyproject,
    )

    manager.update_pyproject_file(pyproject, "1.0.0", verbose=False)

    assert pyproject.read_text() == original


def test_version_manager_update_pyproject_missing_file(tmp_path):
    """update_pyproject_file() handles missing pyproject.toml gracefully."""
    missing = tmp_path / "pyproject.toml"

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[],
        init_files=[],
        pyproject_file=missing,
    )

    # Should not raise
    manager.update_pyproject_file(missing, "1.0.0", verbose=False)


def test_version_manager_update_all_includes_pyproject(tmp_path):
    """update() also updates pyproject.toml [project].version."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "mypkg"\nversion = "0.1.0"\n')
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    init_file.write_text('__version__ = "0.1.0"\n')

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[init_file],
        pyproject_file=pyproject,
    )

    manager.update("3.0.0", verbose=False)

    assert 'version = "3.0.0"' in pyproject.read_text()
    assert version_file.read_text().strip() == "3.0.0"
    assert '__version__ = "3.0.0"' in init_file.read_text()


def test_version_manager_update_skips_pyproject_when_none(tmp_path):
    """update() skips pyproject.toml when pyproject_file=None."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.1.0"\n')
    version_file = tmp_path / "VERSION"

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
        pyproject_file=None,
    )

    manager.update("9.9.9", verbose=False)

    # pyproject.toml should remain unchanged
    assert 'version = "0.1.0"' in pyproject.read_text()
    assert version_file.read_text().strip() == "9.9.9"


def test_version_manager_verify_includes_pyproject(tmp_path):
    """verify() checks pyproject.toml [project].version when present."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "mypkg"\nversion = "1.2.3"\n')
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
        pyproject_file=pyproject,
    )

    assert manager.verify("1.2.3", verbose=False) is True


def test_version_manager_verify_fails_when_pyproject_wrong_version(tmp_path):
    """verify() returns False when pyproject.toml has a mismatched version."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "mypkg"\nversion = "0.0.1"\n')
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
        pyproject_file=pyproject,
    )

    assert manager.verify("1.2.3", verbose=False) is False


def test_version_manager_verify_skips_pyproject_when_none(tmp_path):
    """verify() does not check pyproject.toml when pyproject_file=None."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.0.1"\n')  # wrong version
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
        pyproject_file=None,
    )

    # pyproject.toml mismatch is ignored because pyproject_file=None
    assert manager.verify("1.2.3", verbose=False) is True


def test_version_manager_verify_missing_pyproject_is_skipped(tmp_path):
    """verify() skips pyproject.toml if it does not exist (logs warning)."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n")
    missing_pyproject = tmp_path / "pyproject.toml"

    manager = VersionManager(
        repo_root=tmp_path,
        version_files=[version_file],
        init_files=[],
        pyproject_file=missing_pyproject,
    )

    # Missing pyproject.toml is treated as optional in verify()
    assert manager.verify("1.2.3", verbose=False) is True
