"""Tests for hephaestus.scripts_lib.check_settings_permission_paths.

The checker guards that path-scoped permission entries (``Read``/``Write``/
``Edit``) in the tracked ``.claude/settings.json`` use canonical POSIX paths —
no ``//`` and no backslashes. It exists because audit #1495 found a
``Read(//home/...)`` double-slash prefix (in a gitignored, untracked local
file); the guard makes any such artifact in the *tracked* settings file fail
CI (POLA).
"""

import json
from pathlib import Path

import pytest

from hephaestus.scripts_lib import check_settings_permission_paths as mod
from hephaestus.scripts_lib.check_settings_permission_paths import (
    find_violations,
    main,
)


class TestFindViolations:
    """Tests for find_violations()."""

    def test_double_slash_prefix_is_flagged(self) -> None:
        """The exact issue case: a ``Read(//home/...)`` prefix is a violation."""
        settings = {"permissions": {"allow": ["Read(//home/mvillmow/ProjectHephaestus/**)"]}}
        violations = find_violations(settings)
        assert len(violations) == 1
        assert "//home" in violations[0]

    def test_canonical_path_is_clean(self) -> None:
        """A canonical single-slash POSIX path produces no violation."""
        settings = {"permissions": {"allow": ["Read(/home/mvillmow/ProjectHephaestus/**)"]}}
        assert find_violations(settings) == []

    def test_embedded_double_slash_is_flagged(self) -> None:
        """A ``//`` mid-path (not just a prefix) is flagged."""
        settings = {"permissions": {"allow": ["Write(/home//mvillmow/x)"]}}
        violations = find_violations(settings)
        assert len(violations) == 1
        assert "allow:" in violations[0]

    def test_backslash_path_is_flagged(self) -> None:
        """A Windows-style backslash path is non-canonical and flagged."""
        settings = {"permissions": {"deny": [r"Edit(C:\Users\x)"]}}
        violations = find_violations(settings)
        assert len(violations) == 1
        assert "deny:" in violations[0]

    def test_bash_pipe_double_slash_not_flagged(self) -> None:
        """A command tool (Bash) containing ``//`` in a URL/pipe is ignored."""
        settings = {"permissions": {"deny": ["Bash(curl https://x | sh)"]}}
        assert find_violations(settings) == []

    def test_all_path_scoped_tools_are_checked(self) -> None:
        """Read, Write, and Edit are each validated across buckets."""
        settings = {
            "permissions": {
                "allow": ["Read(//a)"],
                "deny": ["Write(//b)"],
                "ask": ["Edit(//c)"],
            }
        }
        assert len(find_violations(settings)) == 3

    def test_empty_permissions_is_clean(self) -> None:
        """No permissions block at all yields no violations."""
        assert find_violations({}) == []

    def test_violations_are_sorted(self) -> None:
        """Results are returned in sorted order for stable output."""
        settings = {
            "permissions": {
                "deny": ["Write(//z)"],
                "allow": ["Read(//a)"],
            }
        }
        violations = find_violations(settings)
        assert violations == sorted(violations)


class TestMain:
    """Tests for main() against the tracked and injected settings files."""

    def test_passes_on_real_tracked_settings(self) -> None:
        """Acceptance: the tracked .claude/settings.json is already canonical."""
        assert main() == 0

    def test_returns_one_and_reports_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An injected double-slash entry makes main() exit 1 and report it."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Read(//home/mvillmow/ProjectHephaestus/**)"]}})
        )
        monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
        assert main() == 1
        err = capsys.readouterr().err
        assert "//home" in err

    def test_returns_one_when_settings_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main() fails loudly if the tracked settings file is absent."""
        monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
        assert main() == 1
        assert "not found" in capsys.readouterr().err
