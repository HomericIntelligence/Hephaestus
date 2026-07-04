"""Tests for hephaestus/validation/unlinked_todo.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import hephaestus.validation.unlinked_todo as unlinked_todo
from hephaestus.utils.helpers import get_repo_root


class TestScanFile:
    """Tests for the scan_file() per-file marker scanner."""

    def test_bare_todo_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text("x = 1  # TODO: fix later\n")
        v = unlinked_todo.scan_file(f, "m.py")
        assert len(v) == 1
        assert v[0].marker == "TODO"
        assert v[0].line == 1

    def test_linked_todo_allowed(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text("x = 1  # TODO(#710): replace seam\n")
        assert unlinked_todo.scan_file(f, "m.py") == []

    def test_fixme_and_hack_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text("# FIXME broken\n# HACK workaround\n")
        assert {v.marker for v in unlinked_todo.scan_file(f, "m.py")} == {"FIXME", "HACK"}

    def test_word_in_string_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text('label = "TODO list"\n')  # no `#` comment lead-in
        assert unlinked_todo.scan_file(f, "m.py") == []

    def test_comment_marker_inside_string_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text('label = "# TODO"\n')
        assert unlinked_todo.scan_file(f, "m.py") == []

    def test_marker_later_in_comment_text_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text("# linked form uses `# TODO: explanation` syntax\n")
        assert unlinked_todo.scan_file(f, "m.py") == []

    def test_linked_fixme_and_hack_allowed(self, tmp_path: Path) -> None:
        f = tmp_path / "m.py"
        f.write_text("# FIXME(#5): x\n# HACK(#6): y\n")
        assert unlinked_todo.scan_file(f, "m.py") == []


class TestFindViolations:
    """Tests for the find_violations() repo walk."""

    def test_walks_scanned_roots(self, tmp_path: Path) -> None:
        (tmp_path / "hephaestus").mkdir()
        (tmp_path / "hephaestus" / "a.py").write_text("# TODO no link\n")
        v = unlinked_todo.find_violations(tmp_path)
        assert len(v) == 1
        assert v[0].path == "hephaestus/a.py"

    def test_missing_root_skipped(self, tmp_path: Path) -> None:
        # No hephaestus/ or scripts/ dir present -> no findings, no error.
        assert unlinked_todo.find_violations(tmp_path) == []

    def test_scans_self_module_like_any_other_file(self, tmp_path: Path) -> None:
        val = tmp_path / "hephaestus" / "validation"
        val.mkdir(parents=True)
        (val / "unlinked_todo.py").write_text("# TODO bare in own module\n")
        v = unlinked_todo.find_violations(tmp_path)
        assert len(v) == 1
        assert v[0].path == "hephaestus/validation/unlinked_todo.py"
        assert v[0].marker == "TODO"

    def test_real_repo_has_no_unlinked_markers(self) -> None:
        """The shipped tree must pass — the gate is green on main."""
        assert unlinked_todo.find_violations(get_repo_root()) == []


class TestScopeLock:
    """Locks the scanned-roots scope so a silent widening fails loudly."""

    def test_scan_scope_locked(self) -> None:
        """Lock the scanned roots so a silent widening fails loudly."""
        assert unlinked_todo.SCANNED_ROOTS == ("hephaestus", "scripts")


class TestMain:
    """Tests for the main() CLI entry point across its branches."""

    def test_main_ok_on_clean_tree(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = unlinked_todo.main(["--repo-root", str(get_repo_root())])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_main_fails_and_prints_finding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "hephaestus").mkdir()
        (tmp_path / "hephaestus" / "bad.py").write_text("# TODO nope\n")
        rc = unlinked_todo.main(["--repo-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "hephaestus/bad.py:1" in out

    def test_main_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "hephaestus").mkdir()
        (tmp_path / "hephaestus" / "bad.py").write_text("# FIXME x\n")
        rc = unlinked_todo.main(["--json", "--repo-root", str(tmp_path)])
        assert rc == 1
        assert '"violations"' in capsys.readouterr().out

    def test_main_repo_root_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Cover the ``resolve_repo_root`` default branch (flag absent)."""
        monkeypatch.setattr(unlinked_todo, "resolve_repo_root", lambda args: tmp_path)
        assert unlinked_todo.main([]) == 0
