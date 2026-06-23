"""Tests for scripts/check_private_denylist.py."""

from __future__ import annotations

import importlib.util
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


def test_scan_paths_reports_fake_private_token(tmp_path: Path) -> None:
    """A tracked text file containing a denylisted token is reported."""
    source = tmp_path / "hephaestus" / "example.py"
    source.parent.mkdir()
    source.write_text('TOKEN = "PRIVATE_ENDPOINT_TOKEN"\n', encoding="utf-8")

    findings = _mod.scan_paths(tmp_path, [source], ["PRIVATE_ENDPOINT_TOKEN"])

    assert findings == [_mod.Finding(source.relative_to(tmp_path), 1, "PRIVATE_ENDPOINT_TOKEN")]


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


def test_help_flag_prints_doc_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--help must be a zero-exit smoke path."""
    monkeypatch.setattr("sys.argv", ["check_private_denylist.py", "--help"])
    assert _mod.main() == 0
    assert capsys.readouterr().out.strip()
