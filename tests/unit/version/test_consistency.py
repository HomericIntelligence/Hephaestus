#!/usr/bin/env python3

"""Tests for hephaestus.version.consistency module."""

from pathlib import Path

import pytest

from hephaestus.version.consistency import (
    bump_version,
    check_package_version_consistency,
    check_version_consistency,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pyproject(tmp_path: Path, version: str) -> None:
    """Write a minimal pyproject.toml with the given project version."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "test-pkg"\nversion = "{version}"\n'
    )


def _write_pixi(tmp_path: Path, version: str | None) -> None:
    """Write a minimal pixi.toml; omit the version field if None."""
    if version is None:
        (tmp_path / "pixi.toml").write_text('[workspace]\nname = "test-pkg"\n')
    else:
        (tmp_path / "pixi.toml").write_text(
            f'[workspace]\nname = "test-pkg"\nversion = "{version}"\n'
        )


# ---------------------------------------------------------------------------
# check_version_consistency
# ---------------------------------------------------------------------------


def test_check_version_consistency_no_pixi(tmp_path):
    """Pass when pixi.toml is absent."""
    _write_pyproject(tmp_path, "1.2.3")
    assert check_version_consistency(tmp_path) == 0


def test_check_version_consistency_pixi_no_version(tmp_path):
    """Pass when pixi.toml has no [workspace].version."""
    _write_pyproject(tmp_path, "1.2.3")
    _write_pixi(tmp_path, None)
    assert check_version_consistency(tmp_path) == 0


def test_check_version_consistency_matching(tmp_path):
    """Pass when both files have the same version."""
    _write_pyproject(tmp_path, "1.2.3")
    _write_pixi(tmp_path, "1.2.3")
    assert check_version_consistency(tmp_path) == 0


def test_check_version_consistency_mismatch(tmp_path, capsys):
    """Fail when versions differ."""
    _write_pyproject(tmp_path, "1.2.3")
    _write_pixi(tmp_path, "1.2.4")
    result = check_version_consistency(tmp_path)
    assert result == 1
    err = capsys.readouterr().err
    assert "mismatch" in err.lower() or "1.2.3" in err


def test_check_version_consistency_verbose(tmp_path, capsys):
    """Verbose mode prints versions even when consistent."""
    _write_pyproject(tmp_path, "0.5.0")
    result = check_version_consistency(tmp_path, verbose=True)
    assert result == 0
    out = capsys.readouterr().out
    assert "0.5.0" in out


def test_check_version_consistency_missing_pyproject(tmp_path):
    """Exit 1 when pyproject.toml is missing."""
    with pytest.raises(SystemExit) as exc_info:
        check_version_consistency(tmp_path)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# check_package_version_consistency
# ---------------------------------------------------------------------------


def test_check_package_consistency_minimal(tmp_path):
    """Pass with just pyproject.toml and no CHANGELOG."""
    _write_pyproject(tmp_path, "0.3.0")
    assert check_package_version_consistency(tmp_path) == 0


def test_check_package_consistency_init_match(tmp_path):
    """Pass when __init__.py __version__ matches."""
    _write_pyproject(tmp_path, "0.3.0")
    init = tmp_path / "mypkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text('__version__ = "0.3.0"\n')
    assert check_package_version_consistency(tmp_path, package_init=init) == 0


def test_check_package_consistency_init_mismatch(tmp_path, capsys):
    """Fail when __init__.py __version__ does not match."""
    _write_pyproject(tmp_path, "0.3.0")
    init = tmp_path / "mypkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text('__version__ = "0.2.0"\n')
    result = check_package_version_consistency(tmp_path, package_init=init)
    assert result == 1
    err = capsys.readouterr().err
    assert "0.2.0" in err or "mismatch" in err.lower()


def test_check_package_consistency_init_missing(tmp_path):
    """Skip (not fail) when --package-init path does not exist."""
    _write_pyproject(tmp_path, "0.3.0")
    missing = tmp_path / "doesnotexist" / "__init__.py"
    assert check_package_version_consistency(tmp_path, package_init=missing) == 0


def test_check_package_consistency_no_version_in_init(tmp_path):
    """Skip __version__ check when init has no __version__ attribute."""
    _write_pyproject(tmp_path, "0.3.0")
    init = tmp_path / "mypkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text("# no version here\n")
    assert check_package_version_consistency(tmp_path, package_init=init) == 0


def test_check_package_consistency_changelog_ok(tmp_path):
    """Pass when CHANGELOG has no aspirational versions."""
    _write_pyproject(tmp_path, "1.0.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n## [0.9.0]\n- Released\n## [1.0.0]\n- Current\n"
    )
    assert check_package_version_consistency(tmp_path) == 0


def test_check_package_consistency_changelog_aspirational(tmp_path, capsys):
    """Fail when CHANGELOG references a future version."""
    _write_pyproject(tmp_path, "1.0.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n## [1.0.0]\n- Current\n## [1.1.0]\n- Coming soon\n"
    )
    result = check_package_version_consistency(tmp_path)
    assert result == 1
    err = capsys.readouterr().err
    assert "1.1.0" in err


def test_check_package_consistency_changelog_in_codeblock_ignored(tmp_path):
    """Versions inside fenced code blocks are not flagged."""
    _write_pyproject(tmp_path, "1.0.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n## [1.0.0]\nSee:\n```\nversion = 1.9.0\n```\n"
    )
    assert check_package_version_consistency(tmp_path) == 0


def test_check_package_consistency_changelog_in_inline_code_ignored(tmp_path):
    """Versions inside inline code spans are not flagged."""
    _write_pyproject(tmp_path, "1.0.0")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n## [1.0.0]\nSee `version = 9.9.9` for details.\n"
    )
    assert check_package_version_consistency(tmp_path) == 0


def test_check_package_consistency_no_changelog(tmp_path):
    """Pass silently when no CHANGELOG.md exists."""
    _write_pyproject(tmp_path, "0.5.0")
    assert check_package_version_consistency(tmp_path) == 0


# ---------------------------------------------------------------------------
# bump_version
# ---------------------------------------------------------------------------


def _minimal_repo(tmp_path: Path, version: str) -> Path:
    """Set up a minimal repo at tmp_path with pyproject.toml at version."""
    _write_pyproject(tmp_path, version)
    return tmp_path


def test_bump_version_patch(tmp_path):
    """Patch bump increments patch part and resets nothing."""
    _minimal_repo(tmp_path, "1.2.3")
    result = bump_version(tmp_path, "patch", verbose=False)
    assert result == 0
    content = (tmp_path / "pyproject.toml").read_text()
    assert '"1.2.4"' in content


def test_bump_version_minor(tmp_path):
    """Minor bump increments minor and resets patch to 0."""
    _minimal_repo(tmp_path, "1.2.3")
    result = bump_version(tmp_path, "minor", verbose=False)
    assert result == 0
    content = (tmp_path / "pyproject.toml").read_text()
    assert '"1.3.0"' in content


def test_bump_version_major(tmp_path):
    """Major bump increments major and resets minor and patch to 0."""
    _minimal_repo(tmp_path, "1.2.3")
    result = bump_version(tmp_path, "major", verbose=False)
    assert result == 0
    content = (tmp_path / "pyproject.toml").read_text()
    assert '"2.0.0"' in content


def test_bump_version_dry_run(tmp_path):
    """Dry run does not modify any file."""
    _minimal_repo(tmp_path, "1.0.0")
    result = bump_version(tmp_path, "patch", dry_run=True, verbose=False)
    assert result == 0
    # pyproject.toml must be unchanged
    content = (tmp_path / "pyproject.toml").read_text()
    assert '"1.0.0"' in content


def test_bump_version_dry_run_output(tmp_path, capsys):
    """Dry run prints the proposed version change."""
    _minimal_repo(tmp_path, "2.3.4")
    bump_version(tmp_path, "minor", dry_run=True, verbose=False)
    out = capsys.readouterr().out
    assert "2.4.0" in out


def test_bump_version_invalid_part(tmp_path, capsys):
    """Invalid part argument returns exit code 1."""
    _minimal_repo(tmp_path, "1.0.0")
    result = bump_version(tmp_path, "invalid", verbose=False)
    assert result == 1
    err = capsys.readouterr().err
    assert "invalid" in err.lower()


def test_bump_version_missing_pyproject(tmp_path):
    """Missing pyproject.toml causes SystemExit(1)."""
    with pytest.raises(SystemExit) as exc_info:
        bump_version(tmp_path, "patch")
    assert exc_info.value.code == 1


def test_bump_version_verbose(tmp_path, capsys):
    """Verbose mode prints bump message."""
    _minimal_repo(tmp_path, "0.1.0")
    bump_version(tmp_path, "patch", verbose=True)
    out = capsys.readouterr().out
    assert "0.1.1" in out


# ---------------------------------------------------------------------------
# CLI main entry points (smoke tests via monkeypatch of sys.argv)
# ---------------------------------------------------------------------------


def test_check_version_consistency_main_pass(tmp_path, monkeypatch, capsys):
    """CLI passes when pyproject.toml has no matching pixi.toml version."""
    from hephaestus.version.consistency import check_version_consistency_main

    _write_pyproject(tmp_path, "1.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-version-consistency", "--repo-root", str(tmp_path)],
    )
    result = check_version_consistency_main()
    assert result == 0


def test_check_version_consistency_main_mismatch(tmp_path, monkeypatch, capsys):
    """CLI fails when versions differ."""
    from hephaestus.version.consistency import check_version_consistency_main

    _write_pyproject(tmp_path, "1.0.0")
    _write_pixi(tmp_path, "1.0.1")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-version-consistency", "--repo-root", str(tmp_path)],
    )
    result = check_version_consistency_main()
    assert result == 1


def test_check_package_versions_main_pass(tmp_path, monkeypatch):
    """CLI passes with minimal repo and --verbose flag."""
    from hephaestus.version.consistency import check_package_versions_main

    _write_pyproject(tmp_path, "2.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-package-versions", "--repo-root", str(tmp_path), "--verbose"],
    )
    result = check_package_versions_main()
    assert result == 0


def test_bump_version_main_dry_run(tmp_path, monkeypatch, capsys):
    """CLI --dry-run flag prints proposed change without writing."""
    from hephaestus.version.consistency import bump_version_main

    _minimal_repo(tmp_path, "3.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "minor", "--repo-root", str(tmp_path), "--dry-run"],
    )
    result = bump_version_main()
    assert result == 0
    out = capsys.readouterr().out
    assert "3.1.0" in out
    # File must be unchanged
    assert '"3.0.0"' in (tmp_path / "pyproject.toml").read_text()


def test_bump_version_main_patch(tmp_path, monkeypatch):
    """CLI patch bump actually modifies the file."""
    from hephaestus.version.consistency import bump_version_main

    _minimal_repo(tmp_path, "0.5.2")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "patch", "--repo-root", str(tmp_path)],
    )
    result = bump_version_main()
    assert result == 0
    assert '"0.5.3"' in (tmp_path / "pyproject.toml").read_text()


# ---------------------------------------------------------------------------
# check_package_version_consistency with pixi mismatch
# ---------------------------------------------------------------------------


def test_check_package_consistency_pixi_mismatch(tmp_path, capsys):
    """Fail when pixi.toml [workspace].version differs from pyproject.toml."""
    _write_pyproject(tmp_path, "1.0.0")
    _write_pixi(tmp_path, "0.9.0")
    result = check_package_version_consistency(tmp_path)
    assert result == 1
    err = capsys.readouterr().err
    assert "0.9.0" in err


def test_check_package_consistency_pixi_match(tmp_path):
    """Pass when pixi.toml [workspace].version matches pyproject.toml."""
    _write_pyproject(tmp_path, "1.0.0")
    _write_pixi(tmp_path, "1.0.0")
    assert check_package_version_consistency(tmp_path) == 0
