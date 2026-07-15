#!/usr/bin/env python3

"""Tests for hephaestus.version.consistency module.

The project uses hatch-vcs dynamic versioning: the canonical version comes from
git tags, not a file. Tests inject a canonical version by monkeypatching
``_version_from_git_tag`` rather than writing a static ``[project].version``.
"""

from pathlib import Path

import pytest

import hephaestus.version.consistency as consistency
from hephaestus.version.consistency import (
    bump_version,
    check_package_version_consistency,
    check_version_consistency,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def set_canonical(monkeypatch: pytest.MonkeyPatch):
    """Return a callable that pins the canonical (git-tag) version for a test."""

    def _set(version: str) -> None:
        monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: version)

    return _set


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


def test_check_version_consistency_no_pixi(tmp_path, set_canonical):
    """Pass when pixi.toml is absent."""
    set_canonical("1.2.3")
    assert check_version_consistency(tmp_path) == 0


def test_check_version_consistency_pixi_no_version(tmp_path, set_canonical):
    """Pass when pixi.toml has no [workspace].version."""
    set_canonical("1.2.3")
    _write_pixi(tmp_path, None)
    assert check_version_consistency(tmp_path) == 0


def test_check_version_consistency_malformed_pixi_surfaces_diagnostic(tmp_path, set_canonical):
    """Malformed pixi.toml preserves the parser's source diagnostic."""
    set_canonical("1.2.3")
    (tmp_path / "pixi.toml").write_text("[workspace]\nversion =\n")

    with pytest.raises(
        ValueError,
        match=r"Invalid value.*line 2, column \d+",
    ):
        check_version_consistency(tmp_path)


def test_check_version_consistency_matching(tmp_path, set_canonical):
    """Pass when pixi.toml version matches the canonical git-tag version."""
    set_canonical("1.2.3")
    _write_pixi(tmp_path, "1.2.3")
    assert check_version_consistency(tmp_path) == 0


def test_check_version_consistency_mismatch(tmp_path, set_canonical, capsys):
    """Fail when pixi.toml version differs from the canonical version."""
    set_canonical("1.2.3")
    _write_pixi(tmp_path, "1.2.4")
    result = check_version_consistency(tmp_path)
    assert result == 1
    err = capsys.readouterr().err
    assert "mismatch" in err.lower() or "1.2.3" in err


def test_check_version_consistency_verbose(tmp_path, set_canonical, capsys):
    """Verbose mode prints versions even when consistent."""
    set_canonical("0.5.0")
    result = check_version_consistency(tmp_path, verbose=True)
    assert result == 0
    out = capsys.readouterr().out
    assert "0.5.0" in out


def test_check_version_consistency_no_canonical_source(tmp_path, monkeypatch):
    """Exit 1 when no canonical version can be determined (no tag, not installed)."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: None)
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: None)
    with pytest.raises(SystemExit) as exc_info:
        check_version_consistency(tmp_path)
    assert exc_info.value.code == 1


def test_canonical_falls_back_to_metadata(tmp_path, monkeypatch):
    """When no git tag exists, the canonical version falls back to dist metadata."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: None)
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: "7.8.9")
    assert consistency._get_canonical_version(tmp_path) == "7.8.9"


# ---------------------------------------------------------------------------
# check_package_version_consistency
# ---------------------------------------------------------------------------


def test_check_package_consistency_minimal(tmp_path, set_canonical):
    """Pass with no secondary version sources to compare."""
    set_canonical("0.3.0")
    assert check_package_version_consistency(tmp_path) == 0


def test_check_package_consistency_init_match(tmp_path, set_canonical):
    """Pass when __init__.py __version__ matches the canonical version."""
    set_canonical("0.3.0")
    init = tmp_path / "mypkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text('__version__ = "0.3.0"\n')
    assert check_package_version_consistency(tmp_path, package_init=init) == 0


def test_check_package_consistency_init_mismatch(tmp_path, set_canonical, capsys):
    """Fail when __init__.py __version__ does not match the canonical version."""
    set_canonical("0.3.0")
    init = tmp_path / "mypkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text('__version__ = "0.2.0"\n')
    result = check_package_version_consistency(tmp_path, package_init=init)
    assert result == 1
    err = capsys.readouterr().err
    assert "0.2.0" in err or "mismatch" in err.lower()


def test_check_package_consistency_init_missing(tmp_path, set_canonical):
    """Skip (not fail) when --package-init path does not exist."""
    set_canonical("0.3.0")
    missing = tmp_path / "doesnotexist" / "__init__.py"
    assert check_package_version_consistency(tmp_path, package_init=missing) == 0


def test_check_package_consistency_no_version_in_init(tmp_path, set_canonical):
    """Skip __version__ check when init has no __version__ attribute."""
    set_canonical("0.3.0")
    init = tmp_path / "mypkg" / "__init__.py"
    init.parent.mkdir()
    init.write_text("# no version here\n")
    assert check_package_version_consistency(tmp_path, package_init=init) == 0


def test_check_package_consistency_pixi_mismatch(tmp_path, set_canonical, capsys):
    """Fail when pixi.toml [workspace].version differs from the canonical version."""
    set_canonical("1.0.0")
    _write_pixi(tmp_path, "0.9.0")
    result = check_package_version_consistency(tmp_path)
    assert result == 1
    err = capsys.readouterr().err
    assert "0.9.0" in err


def test_check_package_consistency_pixi_match(tmp_path, set_canonical):
    """Pass when pixi.toml [workspace].version matches the canonical version."""
    set_canonical("1.0.0")
    _write_pixi(tmp_path, "1.0.0")
    assert check_package_version_consistency(tmp_path) == 0


def test_check_package_consistency_scan_skills_clean(tmp_path, set_canonical):
    """scan_skills passes when skill markdown has no aspirational versions."""
    set_canonical("1.0.0")
    skills = tmp_path / ".claude-plugin" / "skills"
    skills.mkdir(parents=True)
    (skills / "demo.md").write_text("# Demo skill\n\nNo versions here.\n")
    assert check_package_version_consistency(tmp_path, scan_skills=True) == 0


def test_check_package_consistency_scan_skills_aspirational(tmp_path, set_canonical, capsys):
    """scan_skills fails when a skill references a version above the canonical one."""
    set_canonical("1.0.0")
    skills = tmp_path / ".claude-plugin" / "skills"
    skills.mkdir(parents=True)
    (skills / "demo.md").write_text("# Demo\n\nRequires v2.5.0 of the toolchain.\n")
    result = check_package_version_consistency(tmp_path, scan_skills=True)
    assert result == 1
    assert "2.5.0" in capsys.readouterr().err


def test_check_package_consistency_scan_skills_ignores_code_blocks(tmp_path, set_canonical):
    """Versions inside fenced code blocks are not treated as aspirational."""
    set_canonical("1.0.0")
    skills = tmp_path / ".claude-plugin" / "skills"
    skills.mkdir(parents=True)
    (skills / "demo.md").write_text("# Demo\n\n```\npip install foo==9.9.9\n```\n")
    assert check_package_version_consistency(tmp_path, scan_skills=True) == 0


# ---------------------------------------------------------------------------
# bump_version
# ---------------------------------------------------------------------------


def test_bump_version_patch(tmp_path, set_canonical):
    """Patch bump computes the next patch version."""
    set_canonical("1.2.3")
    result = bump_version(tmp_path, "patch", verbose=False)
    assert result == 0
    assert "1.2.4" in (tmp_path / "VERSION").read_text()


def test_bump_version_minor(tmp_path, set_canonical):
    """Minor bump increments minor and resets patch to 0."""
    set_canonical("1.2.3")
    result = bump_version(tmp_path, "minor", verbose=False)
    assert result == 0
    assert "1.3.0" in (tmp_path / "VERSION").read_text()


def test_bump_version_major(tmp_path, set_canonical):
    """Major bump increments major and resets minor and patch to 0."""
    set_canonical("1.2.3")
    result = bump_version(tmp_path, "major", verbose=False)
    assert result == 0
    assert "2.0.0" in (tmp_path / "VERSION").read_text()


def test_bump_version_dry_run(tmp_path, set_canonical):
    """Dry run does not write a VERSION file."""
    set_canonical("1.0.0")
    result = bump_version(tmp_path, "patch", dry_run=True, verbose=False)
    assert result == 0
    assert not (tmp_path / "VERSION").exists()


def test_bump_version_dry_run_output(tmp_path, set_canonical, capsys):
    """Dry run prints the proposed version change."""
    set_canonical("2.3.4")
    bump_version(tmp_path, "minor", dry_run=True, verbose=False)
    assert "2.4.0" in capsys.readouterr().out


def test_bump_version_invalid_part(tmp_path, set_canonical, capsys):
    """Invalid part argument returns exit code 1."""
    set_canonical("1.0.0")
    result = bump_version(tmp_path, "invalid", verbose=False)
    assert result == 1
    assert "invalid" in capsys.readouterr().err.lower()


def test_bump_version_no_canonical_source(tmp_path, monkeypatch):
    """No determinable canonical version causes SystemExit(1)."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: None)
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: None)
    with pytest.raises(SystemExit) as exc_info:
        bump_version(tmp_path, "patch")
    assert exc_info.value.code == 1


def test_bump_version_verbose(tmp_path, set_canonical, capsys):
    """Verbose mode prints the bump message."""
    set_canonical("0.1.0")
    bump_version(tmp_path, "patch", verbose=True)
    assert "0.1.1" in capsys.readouterr().out


def test_bump_version_next_steps_mention_git_tag(tmp_path, set_canonical, capsys):
    """The post-bump guidance points at git tagging, not editing pyproject.toml."""
    set_canonical("1.0.0")
    bump_version(tmp_path, "patch", verbose=False)
    out = capsys.readouterr().out
    assert "git tag" in out
    assert "v1.0.1" in out


# ---------------------------------------------------------------------------
# CLI main entry points (smoke tests via monkeypatch of sys.argv)
# ---------------------------------------------------------------------------


def test_check_version_consistency_main_pass(tmp_path, set_canonical, monkeypatch):
    """CLI passes when pixi.toml has no conflicting version."""
    from hephaestus.version.consistency import check_version_consistency_main

    set_canonical("1.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-version-consistency", "--repo-root", str(tmp_path)],
    )
    assert check_version_consistency_main() == 0


def test_check_version_consistency_main_mismatch(tmp_path, set_canonical, monkeypatch):
    """CLI fails when versions differ."""
    from hephaestus.version.consistency import check_version_consistency_main

    set_canonical("1.0.0")
    _write_pixi(tmp_path, "1.0.1")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-version-consistency", "--repo-root", str(tmp_path)],
    )
    assert check_version_consistency_main() == 1


def test_check_package_versions_main_pass(tmp_path, set_canonical, monkeypatch):
    """CLI passes with a minimal repo and the --verbose flag."""
    from hephaestus.version.consistency import check_package_versions_main

    set_canonical("2.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-package-versions", "--repo-root", str(tmp_path), "--verbose"],
    )
    assert check_package_versions_main() == 0


def test_bump_version_main_dry_run(tmp_path, set_canonical, monkeypatch, capsys):
    """CLI --dry-run flag prints the proposed change without writing."""
    from hephaestus.version.consistency import bump_version_main

    set_canonical("3.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "minor", "--repo-root", str(tmp_path), "--dry-run"],
    )
    result = bump_version_main()
    assert result == 0
    assert "3.1.0" in capsys.readouterr().out
    assert not (tmp_path / "VERSION").exists()


def test_bump_version_main_patch(tmp_path, set_canonical, monkeypatch):
    """CLI patch bump computes and records the next patch version."""
    from hephaestus.version.consistency import bump_version_main

    set_canonical("0.5.2")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "patch", "--repo-root", str(tmp_path)],
    )
    result = bump_version_main()
    assert result == 0
    assert "0.5.3" in (tmp_path / "VERSION").read_text()
