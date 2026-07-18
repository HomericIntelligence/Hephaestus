"""Tests for the committed project-level denylist source in check_private_denylist.py.

Issue #2179: the local ``.heph-private-denylist`` is optional and, when absent
(the default for CI and fresh clones), the guard is a silent no-op. The
committed ``.heph-project-denylist`` makes the privacy policy centrally
effective for every contributor. These tests cover the merge/dedup loader, the
central-effectiveness path, and the self-skip on the working-tree and staged
scan paths (the tracked pattern file must never flag itself).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_private_denylist.py"
_spec = importlib.util.spec_from_file_location("check_private_denylist", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository for index scan tests."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def test_load_denylist_reads_project_file_when_local_absent(tmp_path: Path) -> None:
    """A committed project token is enforced even with no local file (core criterion)."""
    (tmp_path / ".heph-project-denylist").write_text(
        "# project baseline\nBANNED_PROJECT_PATTERN\n",
        encoding="utf-8",
    )

    assert _mod.load_denylist(tmp_path) == ["BANNED_PROJECT_PATTERN"]


def test_load_denylist_merges_and_dedupes_both_sources(tmp_path: Path) -> None:
    """Both sources merge project-first, order-stable, with duplicates removed."""
    (tmp_path / ".heph-project-denylist").write_text(
        "SHARED_TOKEN\nBANNED_PROJECT_PATTERN\n",
        encoding="utf-8",
    )
    (tmp_path / ".heph-private-denylist").write_text(
        "SHARED_TOKEN\nPRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )

    assert _mod.load_denylist(tmp_path) == [
        "SHARED_TOKEN",
        "BANNED_PROJECT_PATTERN",
        "PRIVATE_ENDPOINT_TOKEN",
    ]


def test_both_files_empty_is_noop(tmp_path: Path) -> None:
    """An absent (or empty) pair of sources leaves the guard a no-op."""
    assert _mod.load_denylist(tmp_path) == []

    (tmp_path / ".heph-project-denylist").write_text("# comments only\n", encoding="utf-8")
    assert _mod.load_denylist(tmp_path) == []


def test_main_flags_project_token_without_local_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() fails on a project token with no local file, and redacts the value."""
    (tmp_path / ".heph-project-denylist").write_text(
        "BANNED_PROJECT_PATTERN\n",
        encoding="utf-8",
    )
    source = tmp_path / "docs" / "example.md"
    source.parent.mkdir()
    source.write_text("This mentions BANNED_PROJECT_PATTERN.\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "get_repo_root", lambda: tmp_path)

    assert _mod.main([str(source)]) == 1

    output = capsys.readouterr().out
    assert "docs/example.md:1" in output
    assert "BANNED_PROJECT_PATTERN" not in output
    assert "intentionally not printed" in output


def test_scan_paths_skips_project_denylist_file(tmp_path: Path) -> None:
    """The committed pattern file is never flagged in the working-tree scan path."""
    (tmp_path / ".heph-project-denylist").write_text(
        "BANNED_PROJECT_PATTERN\n",
        encoding="utf-8",
    )
    tokens = _mod.load_denylist(tmp_path)

    findings = _mod.scan_paths(tmp_path, [tmp_path / ".heph-project-denylist"], tokens)

    assert findings == []


def test_staged_scan_skips_project_denylist_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Staging the populated project file must not self-flag (the central write path).

    Because ``--no-verify`` is banned in this repo, staging a real pattern into
    the tracked ``.heph-project-denylist`` must not be blocked by the guard the
    hook runs, or the central list could never be populated.
    """
    repo = _init_repo(tmp_path)
    (repo / ".heph-project-denylist").write_text(
        "BANNED_PROJECT_PATTERN\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".heph-project-denylist"], cwd=repo, check=True)
    monkeypatch.setattr(_mod, "get_repo_root", lambda: repo)

    assert _mod.main(["--staged"]) == 0
