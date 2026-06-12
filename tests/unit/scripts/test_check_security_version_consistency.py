"""Tests for scripts/check_security_version_consistency.py."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_security_version_consistency import (
    GIT_TAG_CMD,
    extract_table_rows,
    latest_release_minor,
    main,
)


class TestExtractTableRows:
    """Tests for extract_table_rows function."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param(
                "| 0.9.x | ✅ Supported    |\n| < 0.9 | ❌ End of life |\n",
                (["0.9"], ["0.9"]),
                id="canonical_rows",
            ),
            pytest.param(
                "|0.9.x|✅ Supported|\n|< 0.9|❌ EOL|\n",
                (["0.9"], ["0.9"]),
                id="no_padding",
            ),
            pytest.param(
                "| 1.0.x | ✅ |\n| < 1.0 | ❌ |\n",
                (["1.0"], ["1.0"]),
                id="post_1_0",
            ),
            pytest.param(
                "| 1.10.x | ✅ |\n| < 1.10 | ❌ |\n",
                (["1.10"], ["1.10"]),
                id="multi_digit_minor",
            ),
        ],
    )
    def test_parses(self, content, expected):
        assert extract_table_rows(content) == expected

    def test_no_table_returns_empty_lists(self):
        assert extract_table_rows("no table here") == ([], [])

    def test_only_supported_row(self):
        content = "| 0.9.x | ✅ Supported |\n"
        supported, eol = extract_table_rows(content)
        assert supported == ["0.9"]
        assert eol == []

    def test_only_eol_row(self):
        content = "| < 0.9 | ❌ EOL |\n"
        supported, eol = extract_table_rows(content)
        assert supported == []
        assert eol == ["0.9"]

    def test_multiple_supported_rows(self):
        content = "| 1.0.x | ✅ Supported |\n| 0.9.x | ✅ Supported |\n| < 0.9 | ❌ EOL |\n"
        supported, eol = extract_table_rows(content)
        assert supported == ["1.0", "0.9"]
        assert eol == ["0.9"]


class TestLatestReleaseMinor:
    """Tests for latest_release_minor function."""

    def test_picks_highest_semver_tag(self, tmp_path):
        with patch("check_security_version_consistency.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "v0.9.4\nv0.9.0\nv0.8.0\n"
            assert latest_release_minor(tmp_path) == "0.9"

    def test_ignores_non_semver(self, tmp_path):
        with patch("check_security_version_consistency.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "release-1\nv0.9.0-rc1\nv0.9.0\n"
            assert latest_release_minor(tmp_path) == "0.9"

    def test_multi_digit_minor_ordering(self, tmp_path):
        # git --sort=-v:refname yields v1.10.0 before v1.9.0; ensure script honors it
        with patch("check_security_version_consistency.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "v1.10.0\nv1.9.0\nv1.0.0\n"
            assert latest_release_minor(tmp_path) == "1.10"

    def test_no_tags(self, tmp_path):
        with patch("check_security_version_consistency.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            assert latest_release_minor(tmp_path) is None

    def test_git_command_invoked_with_expected_args(self, tmp_path):
        with patch("check_security_version_consistency.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "v0.9.0\n"
            latest_release_minor(tmp_path)
            args = run.call_args[0][0]
            assert args == ["git", "-C", str(tmp_path), *GIT_TAG_CMD]
            assert "--sort=-v:refname" in args
            assert "v[0-9]*.*" in args


class TestMain:
    """Tests for main function."""

    def _write_canonical(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "SECURITY.md").write_text(
            "| 0.9.x | ✅ Supported    |\n| < 0.9 | ❌ End of life |\n"
        )

    def test_ok_when_aligned(self, tmp_path, monkeypatch, capsys):
        self._write_canonical(tmp_path)
        monkeypatch.setattr("check_security_version_consistency.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            "check_security_version_consistency.latest_release_minor",
            lambda _r: "0.9",
        )
        assert main() == 0
        assert "OK" in capsys.readouterr().out

    def test_fails_when_drifted(self, tmp_path, monkeypatch, capsys):
        self._write_canonical(tmp_path)
        monkeypatch.setattr("check_security_version_consistency.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            "check_security_version_consistency.latest_release_minor",
            lambda _r: "1.0",
        )
        assert main() == 1
        assert "out of sync" in capsys.readouterr().out

    def test_skips_when_no_tags(self, tmp_path, monkeypatch, capsys):
        self._write_canonical(tmp_path)
        monkeypatch.setattr("check_security_version_consistency.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            "check_security_version_consistency.latest_release_minor",
            lambda _r: None,
        )
        assert main() == 0
        assert "WARNING" in capsys.readouterr().out

    def test_fails_when_multiple_supported_rows(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "SECURITY.md").write_text(
            "| 1.0.x | ✅ Supported |\n| 0.9.x | ✅ Supported |\n| < 0.9 | ❌ EOL |\n"
        )
        monkeypatch.setattr("check_security_version_consistency.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            "check_security_version_consistency.latest_release_minor",
            lambda _r: "1.0",
        )
        assert main() == 1
        out = capsys.readouterr().out
        assert "exactly ONE supported" in out
        assert "multi-series" in out

    def test_fails_when_no_eol_row(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "SECURITY.md").write_text("| 0.9.x | ✅ Supported |\n")
        monkeypatch.setattr("check_security_version_consistency.get_repo_root", lambda: tmp_path)
        monkeypatch.setattr(
            "check_security_version_consistency.latest_release_minor",
            lambda _r: "0.9",
        )
        assert main() == 1
        assert "exactly ONE EOL" in capsys.readouterr().out
