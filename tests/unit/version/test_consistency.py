#!/usr/bin/env python3

"""Tests for hephaestus.version.consistency module.

The project uses hatch-vcs dynamic versioning: the canonical version comes from
git tags, not a file. Tests inject a canonical version by monkeypatching
``_version_from_git_tag`` rather than writing a static ``[project].version``.
"""

import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

import hephaestus.version.consistency as consistency
from hephaestus.version import bump_version, check_version_consistency
from hephaestus.version.consistency import (
    check_package_version_consistency,
    preview_version,
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


# ---------------------------------------------------------------------------
# check_version_consistency
# ---------------------------------------------------------------------------


def test_version_from_git_tag_passes_metadata_timeout(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The git-tag probe forwards the shared metadata timeout."""
    completed = subprocess.CompletedProcess([], 0, stdout="v1.2.3\n", stderr="")
    run = Mock(return_value=completed)
    monkeypatch.setattr(consistency, "METADATA_TIMEOUT", 17)
    monkeypatch.setattr(subprocess, "run", run)

    assert consistency._version_from_git_tag(tmp_path) == "1.2.3"
    assert run.call_args.kwargs["timeout"] == 17


def test_version_from_git_tag_timeout_warns_and_returns_none(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """A git timeout fails closed with a bounded, fixed diagnostic."""
    hostile = "secret-" * 20_000
    error = subprocess.TimeoutExpired(
        cmd=[hostile],
        timeout=10,
        output=hostile,
        stderr=hostile,
    )
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))

    assert consistency._version_from_git_tag(tmp_path) is None
    captured = capsys.readouterr()
    assert captured.err == (
        "[hephaestus-version] WARNING: git tag lookup timed out; using installed package metadata\n"
    )
    assert hostile not in captured.err


def test_check_requested_version_requires_exact_requested_versions(tmp_path, monkeypatch):
    """Pass when both independent version sources exactly match the request."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: "1.2.3")
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: "1.2.3")

    assert consistency._verify_requested_version(tmp_path, "1.2.3") == 0


@pytest.mark.parametrize(
    ("canonical", "installed"),
    [
        ("1.2.2", "1.2.3"),
        ("1.2.3", "1.2.2"),
        ("1.2.3", "1.2.3.dev1"),
        (None, "1.2.3"),
        ("1.2.3", None),
    ],
)
def test_check_requested_version_rejects_source_mismatch(
    tmp_path, monkeypatch, canonical, installed
):
    """Reject stale, missing, or development-version sources."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: canonical)
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: installed)

    assert consistency._verify_requested_version(tmp_path, "1.2.3") == 1


def test_check_requested_version_rejects_invalid_requested_version(tmp_path, capsys):
    """Reject requests that are not exact three-part semantic versions."""
    assert consistency._verify_requested_version(tmp_path, "1.2") == 1
    assert "Invalid version format" in capsys.readouterr().err


def test_check_requested_version_verbose(tmp_path, monkeypatch, capsys):
    """Verbose mode prints both exact sources when they match."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: "0.5.0")
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: "0.5.0")

    result = consistency._verify_requested_version(tmp_path, "0.5.0", verbose=True)
    assert result == 0
    out = capsys.readouterr().out
    assert "Canonical tag version: 0.5.0" in out
    assert "Installed distribution version: 0.5.0" in out


def test_check_requested_version_no_matching_sources(tmp_path, monkeypatch):
    """Return failure when neither source matches the requested version."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: None)
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: None)

    assert consistency._verify_requested_version(tmp_path, "1.2.3") == 1


def test_check_version_consistency_preserves_legacy_signature(tmp_path, set_canonical, capsys):
    """The exported status check accepts both historical calling forms."""
    set_canonical("1.2.3")

    assert check_version_consistency(tmp_path) == 0
    assert capsys.readouterr().out == ""

    assert check_version_consistency(tmp_path, True) == 0
    assert "Canonical version (git tag): 1.2.3" in capsys.readouterr().out


def test_canonical_requires_git_tag(tmp_path, monkeypatch):
    """Installed metadata cannot replace the authoritative Git tag."""
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: None)
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: "7.8.9")

    with pytest.raises(SystemExit) as exc_info:
        consistency._get_canonical_version(tmp_path)
    assert exc_info.value.code == 1


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


@pytest.mark.parametrize(
    ("part", "expected"),
    [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")],
)
def test_public_bump_version_computes_without_mutation(tmp_path, set_canonical, part, expected):
    """The exported legacy wrapper returns success without changing files."""
    set_canonical("1.2.3")

    assert preview_version(tmp_path, part) == expected
    assert bump_version(tmp_path, part, dry_run=True) == 0
    assert list(tmp_path.iterdir()) == []


def test_bump_version_preserves_secondary_version_files(tmp_path, set_canonical):
    """A preview leaves legacy version files byte-for-byte unchanged."""
    set_canonical("1.2.3")
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    pyproject = tmp_path / "pyproject.toml"
    version_file.write_text("legacy\n")
    init_file.write_text('__version__ = "legacy"\n')
    pyproject.write_text('[project]\nversion = "legacy"\n')
    before = {path: path.read_bytes() for path in (version_file, init_file, pyproject)}

    assert preview_version(tmp_path, "minor") == "1.3.0"
    assert bump_version(tmp_path, "minor") == 0
    assert {path: path.read_bytes() for path in before} == before


def test_bump_version_failure_preserves_repository_files(tmp_path, monkeypatch):
    """Failure to resolve the tag leaves existing repository files unchanged."""
    sentinel = tmp_path / "VERSION"
    sentinel.write_text("unchanged\n")
    monkeypatch.setattr(consistency, "_version_from_git_tag", lambda _root: None)

    with pytest.raises(SystemExit):
        bump_version(tmp_path, "patch")

    assert sentinel.read_text() == "unchanged\n"
    assert list(tmp_path.iterdir()) == [sentinel]


def test_bump_version_invalid_part(tmp_path, set_canonical):
    """Invalid bump parts fail without changing repository state."""
    set_canonical("1.0.0")

    assert bump_version(tmp_path, "invalid") == 1
    assert list(tmp_path.iterdir()) == []


def test_bump_version_verbose(tmp_path, set_canonical, capsys):
    """Verbose mode reports the computed transition."""
    set_canonical("0.1.0")

    assert bump_version(tmp_path, "patch", verbose=True) == 0
    assert "Computed next version: 0.1.0 -> 0.1.1" in capsys.readouterr().out


def test_bump_version_refuses_dynamic_project_before_tag_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The library wrapper refuses dynamic projects before resolving a tag."""
    pyproject = tmp_path / "pyproject.toml"
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    pyproject.write_text(
        '[project]\ndynamic = ["version"]\n\n[tool.hatch.version]\nsource = "vcs"\n'
    )
    version_file.write_text("1.2.3\n")
    init_file.write_text('__version__ = "1.2.3"\n')
    before = {path: path.read_bytes() for path in (pyproject, version_file, init_file)}
    monkeypatch.setattr(
        consistency,
        "_get_canonical_version",
        lambda _root: pytest.fail("dynamic-project preflight must precede tag lookup"),
    )

    assert consistency.bump_version(tmp_path, "patch", verbose=True) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "signed Auto Tag Release workflow" in captured.err
    assert {path: path.read_bytes() for path in before} == before


def test_bump_version_refuses_single_quoted_dynamic_project_before_tag_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid TOML literal string cannot bypass the dynamic-version preflight."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\ndynamic = ['version']\n\n[tool.hatch.version]\nsource = 'vcs'\n"
    )
    monkeypatch.setattr(
        consistency,
        "_get_canonical_version",
        lambda _root: pytest.fail("dynamic-project preflight must precede tag lookup"),
    )

    assert consistency.bump_version(tmp_path, "patch", verbose=True) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "signed Auto Tag Release workflow" in captured.err
    assert not (tmp_path / "VERSION").exists()


@pytest.mark.parametrize("extra_args", [[], ["--dry-run"]])
def test_bump_version_main_refuses_dynamic_project_without_output(
    tmp_path: Path,
    extra_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human CLI modes fail closed without reading tags or writing static state."""
    from hephaestus.version.consistency import bump_version_main

    pyproject = tmp_path / "pyproject.toml"
    version_file = tmp_path / "VERSION"
    init_file = tmp_path / "__init__.py"
    pyproject.write_text(
        '[project]\ndynamic = ["version"]\n\n[tool.hatch.version]\nsource = "vcs"\n'
    )
    version_file.write_text("1.2.3\n")
    init_file.write_text('__version__ = "1.2.3"\n')
    before = {path: path.read_bytes() for path in (pyproject, version_file, init_file)}
    monkeypatch.setattr(
        consistency,
        "_get_canonical_version",
        lambda _root: pytest.fail("dynamic-project preflight must precede tag lookup"),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "patch", "--repo-root", str(tmp_path), *extra_args],
    )

    assert bump_version_main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "signed Auto Tag Release workflow" in captured.err
    assert {path: path.read_bytes() for path in before} == before


def test_bump_version_main_json_refusal_is_single_valid_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON refusal output remains parseable and keeps guidance on stderr."""
    from hephaestus.version.consistency import bump_version_main

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndynamic = ["version"]\n\n[tool.hatch.version]\nsource = "vcs"\n'
    )
    monkeypatch.setattr(
        consistency,
        "_get_canonical_version",
        lambda _root: pytest.fail("dynamic-project preflight must precede tag lookup"),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "hephaestus-bump-version",
            "patch",
            "--repo-root",
            str(tmp_path),
            "--dry-run",
            "--json",
        ],
    )

    assert bump_version_main() == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "status": "error",
        "exit_code": 1,
        "message": "version bump refused",
    }
    assert "signed Auto Tag Release workflow" in captured.err
    assert not (tmp_path / "VERSION").exists()


# ---------------------------------------------------------------------------
# CLI main entry points (smoke tests via monkeypatch of sys.argv)
# ---------------------------------------------------------------------------


def test_check_version_consistency_main_pass(tmp_path, set_canonical, monkeypatch):
    """CLI passes with the canonical dynamic version configuration."""
    from hephaestus.version.consistency import check_version_consistency_main

    set_canonical("1.0.0")
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: "1.0.0")
    monkeypatch.setattr(
        "sys.argv",
        [
            "hephaestus-check-version-consistency",
            "--repo-root",
            str(tmp_path),
            "--expected-version",
            "1.0.0",
        ],
    )
    assert check_version_consistency_main() == 0


def test_check_version_consistency_main_json_reports_exact_sources(
    tmp_path, set_canonical, monkeypatch, capsys
):
    """JSON consistency output includes both exact verification sources."""
    from hephaestus.version.consistency import check_version_consistency_main

    set_canonical("1.0.0")
    monkeypatch.setattr(consistency, "_version_from_metadata", lambda: "1.0.0")
    monkeypatch.setattr(
        "sys.argv",
        [
            "hephaestus-check-version-consistency",
            "--repo-root",
            str(tmp_path),
            "--expected-version",
            "1.0.0",
            "--json",
        ],
    )

    assert check_version_consistency_main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "expected_version": "1.0.0",
        "canonical_version": "1.0.0",
        "installed_version": "1.0.0",
        "consistent": True,
    }


def test_check_package_versions_main_pass(tmp_path, set_canonical, monkeypatch):
    """CLI passes with a minimal repo and the --verbose flag."""
    from hephaestus.version.consistency import check_package_versions_main

    set_canonical("2.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-check-package-versions", "--repo-root", str(tmp_path), "--verbose"],
    )
    assert check_package_versions_main() == 0


def test_bump_version_main_compute_only(tmp_path, set_canonical, monkeypatch, capsys):
    """CLI prints the proposed change without writing repository files."""
    from hephaestus.version.consistency import bump_version_main

    set_canonical("3.0.0")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "minor", "--repo-root", str(tmp_path)],
    )
    result = bump_version_main()
    assert result == 0
    assert "Computed next version: 3.1.0" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_bump_version_main_json_reports_compute_only(tmp_path, set_canonical, monkeypatch, capsys):
    """JSON output identifies the proposal and confirms no mutation occurred."""
    from hephaestus.version.consistency import bump_version_main

    set_canonical("0.5.2")
    monkeypatch.setattr(
        "sys.argv",
        ["hephaestus-bump-version", "patch", "--repo-root", str(tmp_path), "--json"],
    )
    assert bump_version_main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "requested_version": "0.5.3",
        "changed": False,
        "authoritative_action": "Auto Tag Release workflow",
    }
    assert list(tmp_path.iterdir()) == []
