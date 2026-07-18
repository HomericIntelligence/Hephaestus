"""Focused tests for commit orchestration helpers in ``pr_manager``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from hephaestus.automation import pr_manager


def _status(stdout: str = "") -> MagicMock:
    """Build a mocked completed subprocess result."""
    return MagicMock(stdout=stdout)


class TestReadPorcelainStatus:
    """Tests for reading stable worktree status."""

    def test_requests_nul_delimited_porcelain_v1(self) -> None:
        worktree_path = Path("/tmp/worktree")
        with patch.object(pr_manager, "run", return_value=_status("?? file.py\0")) as run_mock:
            status = pr_manager._read_porcelain_status(worktree_path, git_timeout=17)

        assert status == "?? file.py\0"
        run_mock.assert_called_once_with(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=worktree_path,
            capture_output=True,
            timeout=17,
        )

    def test_rejects_undecodable_status_output(self) -> None:
        decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with patch.object(pr_manager, "run", side_effect=decode_error):
            with pytest.raises(RuntimeError, match="Could not decode"):
                pr_manager._read_porcelain_status(Path("/tmp/worktree"), git_timeout=None)


class TestParsePorcelainStatus:
    """Tests for NUL-delimited porcelain-v1 parsing."""

    @pytest.mark.parametrize(
        ("porcelain", "expected_path"),
        [
            ('?? src/quote"name.py\0', 'src/quote"name.py'),
            ("?? src/back\\slash.py\0", "src/back\\slash.py"),
            ("?? src/tab\tname.py\0", "src/tab\tname.py"),
            ("?? src/line\nbreak.py\0", "src/line\nbreak.py"),
            ("?? docs/café.md\0", "docs/café.md"),
        ],
    )
    def test_preserves_literal_paths(self, porcelain: str, expected_path: str) -> None:
        assert pr_manager._parse_porcelain_status(porcelain) == (("??", expected_path),)

    def test_uses_rename_destination_and_consumes_source(self) -> None:
        porcelain = 'R  dst/quote"name.py\0src/back\\slash.py\0'

        assert pr_manager._parse_porcelain_status(porcelain) == (("R ", 'dst/quote"name.py'),)

    @pytest.mark.parametrize("status", (" A", " T", " R", " C", "UA", "AA"))
    def test_accepts_documented_status_pairs(self, status: str) -> None:
        records = [f"{status} valid-path.py"]
        if "R" in status or "C" in status:
            records.append("source-path.py")

        assert pr_manager._parse_porcelain_status("\0".join(records) + "\0") == (
            (status, "valid-path.py"),
        )

    @pytest.mark.parametrize(
        "porcelain",
        [
            "?? missing-terminator.py",
            "? malformed.py\0",
            "?? \0",
            "R  destination.py\0",
            "\0?? accepted-after-empty.py\0",
            "?? first.py\0\0",
            "ZZ invalid-status.py\0",
            "R? invalid-status-combination.py\0source.py\0",
        ],
    )
    def test_rejects_malformed_records(self, porcelain: str) -> None:
        with pytest.raises(RuntimeError, match="Malformed"):
            pr_manager._parse_porcelain_status(porcelain)


class TestSelectCommitPaths:
    """Tests for applying staging policy to parsed paths."""

    def test_filters_secrets_and_unallowlisted_paths(self) -> None:
        entries = (
            (" M", "src/keep.py"),
            ("??", ".env"),
            (" D", "src/delete.py"),
            ("??", "scratch.log"),
            ("??", "credentials.json"),
        )

        paths = pr_manager._select_commit_paths(
            entries,
            allowed_paths=("src/keep.py", ".env", "src/delete.py", "credentials.json"),
        )

        assert paths == pr_manager._CommitPaths(
            add_paths=("src/keep.py",),
            update_paths=("src/delete.py",),
        )
        with pytest.raises(FrozenInstanceError):
            paths.add_paths = ()  # type: ignore[misc]


class TestStageCommitPaths:
    """Tests for staging selected paths."""

    def test_stages_deleted_paths_before_regular_paths_with_timeout(self) -> None:
        paths = pr_manager._CommitPaths(
            add_paths=("src/add.py", 'src/quote"name.py'),
            update_paths=("src/delete.py",),
        )
        worktree_path = Path("/tmp/worktree")
        with patch.object(pr_manager, "run") as run_mock:
            pr_manager._stage_commit_paths(paths, worktree_path, git_timeout=19)

        assert run_mock.call_args_list == [
            call(
                ["git", "add", "-u", "--", "src/delete.py"],
                cwd=worktree_path,
                timeout=19,
            ),
            call(
                ["git", "add", "--", "src/add.py", 'src/quote"name.py'],
                cwd=worktree_path,
                timeout=19,
            ),
        ]


class TestCommitWithSignature:
    """Tests for the final repository-policy commit command."""

    def test_signs_and_signs_off_commit_with_timeout(self) -> None:
        worktree_path = Path("/tmp/worktree")
        with patch.object(pr_manager, "run") as run_mock:
            pr_manager._commit_with_signature("refactor: split commit helper", worktree_path, 23)

        run_mock.assert_called_once_with(
            ["git", "commit", "-S", "-s", "-m", "refactor: split commit helper"],
            cwd=worktree_path,
            timeout=23,
        )
