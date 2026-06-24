"""Tests for scripts/check_private_denylist.py."""

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


def test_missing_denylist_is_noop(tmp_path: Path) -> None:
    """The guard should not affect contributors without local private tokens."""
    assert _mod.load_denylist(tmp_path) == []


def test_load_denylist_ignores_blank_lines_and_comments(tmp_path: Path) -> None:
    """Operators can annotate the local denylist without creating patterns."""
    (tmp_path / ".heph-private-denylist").write_text(
        "\n# local only\nPRIVATE_ENDPOINT_TOKEN\n\nPRIVATE_MODEL_ALIAS\n",
        encoding="utf-8",
    )

    assert _mod.load_denylist(tmp_path) == ["PRIVATE_ENDPOINT_TOKEN", "PRIVATE_MODEL_ALIAS"]


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository for index scan tests."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def test_scan_paths_reports_fake_private_token_without_storing_it(tmp_path: Path) -> None:
    """A tracked text file containing a denylisted token is reported redacted."""
    source = tmp_path / "hephaestus" / "example.py"
    source.parent.mkdir()
    source.write_text('TOKEN = "PRIVATE_ENDPOINT_TOKEN"\n', encoding="utf-8")

    findings = _mod.scan_paths(tmp_path, [source], ["PRIVATE_ENDPOINT_TOKEN"])

    assert findings == [_mod.Finding("working-tree", source.relative_to(tmp_path), 1)]
    assert "PRIVATE_ENDPOINT_TOKEN" not in repr(findings)


def test_main_returns_nonzero_when_fake_token_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() should fail when any scanned file contains a local denylist token."""
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )
    source = tmp_path / "docs" / "example.md"
    source.parent.mkdir()
    source.write_text("This mentions PRIVATE_ENDPOINT_TOKEN.\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "get_repo_root", lambda: tmp_path)

    assert _mod.main([str(source)]) == 1


def test_main_redacts_private_tokens_from_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Diagnostics should identify location without printing private values."""
    (tmp_path / ".heph-private-denylist").write_text(
        "PRIVATE_ENDPOINT_TOKEN\n",
        encoding="utf-8",
    )
    source = tmp_path / "docs" / "example.md"
    source.parent.mkdir()
    source.write_text("This mentions PRIVATE_ENDPOINT_TOKEN.\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "get_repo_root", lambda: tmp_path)

    assert _mod.main([str(source)]) == 1

    output = capsys.readouterr().out
    assert "docs/example.md:1" in output
    assert "PRIVATE_ENDPOINT_TOKEN" not in output
    assert "intentionally not printed" in output


def test_staged_scan_reads_index_content_not_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--staged should scan index blobs instead of mutable working-tree files."""
    repo = _init_repo(tmp_path)
    (repo / ".heph-private-denylist").write_text("PRIVATE_MODEL_ALIAS\n", encoding="utf-8")
    source = repo / "docs" / "pi.md"
    source.parent.mkdir()
    source.write_text("PRIVATE_MODEL_ALIAS\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/pi.md"], cwd=repo, check=True)
    source.write_text("clean working tree copy\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "get_repo_root", lambda: repo)

    assert _mod.main(["--staged"]) == 1

    output = capsys.readouterr().out
    assert "staged docs/pi.md:1" in output
    assert "PRIVATE_MODEL_ALIAS" not in output


def test_help_flag_prints_doc_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--help must be a zero-exit smoke path."""
    monkeypatch.setattr("sys.argv", ["check_private_denylist.py", "--help"])
    assert _mod.main() == 0
    assert capsys.readouterr().out.strip()
