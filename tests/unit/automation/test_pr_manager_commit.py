"""Focused tests for commit orchestration helpers in ``pr_manager``."""

from __future__ import annotations

import logging
import subprocess
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
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            ],
            cwd=worktree_path,
            capture_output=True,
            timeout=17,
        )

    def test_rejects_undecodable_status_output(self) -> None:
        decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with patch.object(pr_manager, "run", side_effect=decode_error):
            with pytest.raises(RuntimeError, match="Could not decode"):
                pr_manager._read_porcelain_status(Path("/tmp/worktree"), git_timeout=None)

    def test_enumerates_nested_untracked_files_before_secret_filtering(
        self, tmp_path: Path
    ) -> None:
        """A new directory cannot hide a secret file behind one status entry."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=tmp_path,
            check=True,
        )
        tracked = tmp_path / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", tracked.name], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "--no-gpg-sign", "-m", "test: base"],
            cwd=tmp_path,
            check=True,
        )
        nested = tmp_path / "new"
        nested.mkdir()
        (nested / ".env").write_text("TOP_SECRET=1\n", encoding="utf-8")
        (nested / "safe.txt").write_text("safe\n", encoding="utf-8")

        status = pr_manager._read_porcelain_status(tmp_path, git_timeout=10)
        paths = pr_manager._select_commit_paths(pr_manager._parse_porcelain_status(status), None)
        pr_manager._stage_commit_paths(paths, tmp_path, 10)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")

        assert b"new/safe.txt" in staged
        assert b"new/.env" not in staged


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

    def test_accepts_worktree_type_change_status(self) -> None:
        """Regression (#2228): a worktree-only type change ``" T"`` is valid.

        Git reports a file whose type changed in the worktree (e.g. a regular
        file replaced by a symlink) with an unstaged ``" T"`` status. PR #2208
        added it to ``_PORCELAIN_STATUS_PAIRS``; this pins the behavior so a
        future edit to that allowlist cannot silently reject the entry again.
        """
        porcelain = " T src/type-changed.py\0"

        assert pr_manager._parse_porcelain_status(porcelain) == ((" T", "src/type-changed.py"),)

    @pytest.mark.parametrize("status", ("TA", "TR", "TC"))
    def test_rejects_invalid_type_change_status(self, status: str) -> None:
        """An index type-change paired with add/rename/copy is not a valid pair."""
        with pytest.raises(RuntimeError, match="Malformed"):
            pr_manager._parse_porcelain_status(f"{status} src/type-changed.py\0")


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

    @pytest.mark.parametrize("status", ("DD", "AU", "UD", "UA", "DU", "AA", "UU"))
    def test_rejects_unmerged_delete_states(self, status: str) -> None:
        """An unresolved file cannot become an automated deletion."""
        with pytest.raises(RuntimeError, match="unresolved merge"):
            pr_manager._select_commit_paths(((status, "conflict.txt"),), None)

    def test_escapes_control_characters_in_skip_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Path payloads preserved by _parse_porcelain_status (PR #2208) may carry
        # newlines / terminal control chars; skip logs must render them escaped so a
        # filename cannot forge a log line or emit terminal escapes (issue #2231).
        newline_path = "src/forged\nINJECTED log line.py"
        # Basename stays ``.env`` so the secret (warning) branch fires on a real
        # secret name while the control payload rides in a leading path segment.
        control_path = "sub/\x1b]0;pwned\x07/.env"
        entries = (
            (" M", newline_path),
            ("??", control_path),
        )

        with caplog.at_level(logging.DEBUG):
            paths = pr_manager._select_commit_paths(entries, allowed_paths=(control_path,))

        # Selection semantics unchanged: newline_path filtered (not allowlisted),
        # control_path filtered (secret .env basename), nothing staged.
        assert paths == pr_manager._CommitPaths(add_paths=(), update_paths=())

        skip_messages = [record.getMessage() for record in caplog.records]
        assert any(repr(newline_path) in message for message in skip_messages)
        assert any(repr(control_path) in message for message in skip_messages)
        # Each untrusted value is a single escaped record — no raw newline or ESC leaks.
        for message in skip_messages:
            assert "\n" not in message
            assert "\x1b" not in message


class TestStageCommitPaths:
    """Tests for staging selected paths."""

    def test_stages_deleted_paths_before_regular_paths_with_timeout(self) -> None:
        paths = pr_manager._CommitPaths(
            add_paths=("src/add.py", 'src/quote"name.py'),
            update_paths=("src/delete.py",),
        )
        worktree_path = Path("/tmp/worktree")
        with patch.object(
            pr_manager,
            "run",
            side_effect=[_status("src/delete.py\0"), _status(), _status(), _status()],
        ) as run_mock:
            pr_manager._stage_commit_paths(paths, worktree_path, git_timeout=19)

        assert run_mock.call_args_list == [
            call(
                [
                    "git",
                    "--literal-pathspecs",
                    "ls-tree",
                    "-r",
                    "-z",
                    "--name-only",
                    "HEAD",
                    "--",
                    "src/delete.py",
                ],
                cwd=worktree_path,
                timeout=19,
            ),
            call(
                ["git", "read-tree", "HEAD"],
                cwd=worktree_path,
                timeout=19,
            ),
            call(
                [
                    "git",
                    "--literal-pathspecs",
                    "update-index",
                    "--force-remove",
                    "--",
                    "src/delete.py",
                ],
                cwd=worktree_path,
                timeout=19,
            ),
            call(
                [
                    "git",
                    "--literal-pathspecs",
                    "add",
                    "--",
                    "src/add.py",
                    'src/quote"name.py',
                ],
                cwd=worktree_path,
                timeout=19,
            ),
        ]

    def test_discards_a_prestaged_secret_before_staging_safe_paths(self, tmp_path: Path) -> None:
        """A selected safe change cannot carry a pre-staged secret into its tree."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=tmp_path,
            check=True,
        )
        secret = tmp_path / ".env"
        safe = tmp_path / "safe.txt"
        secret.write_text("BASE=1\n", encoding="utf-8")
        safe.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", ".env", "safe.txt"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "--no-gpg-sign", "-m", "test: base"],
            cwd=tmp_path,
            check=True,
        )
        secret.write_text("TOP_SECRET=1\n", encoding="utf-8")
        safe.write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "add", ".env"], cwd=tmp_path, check=True)

        status = pr_manager._read_porcelain_status(tmp_path, git_timeout=10)
        paths = pr_manager._select_commit_paths(pr_manager._parse_porcelain_status(status), None)
        pr_manager._stage_commit_paths(paths, tmp_path, 10)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")

        assert b"safe.txt" in staged
        assert b".env" not in staged

    def test_stages_only_the_source_deletion_after_a_staged_rename_is_deleted(
        self, tmp_path: Path
    ) -> None:
        """A deleted staged rename target cannot prevent the source deletion."""
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        source = tmp_path / "source.txt"
        target = tmp_path / "target.txt"
        source.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", source.name], cwd=tmp_path, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-q",
                "--no-gpg-sign",
                "-m",
                "test: base",
            ],
            cwd=tmp_path,
            check=True,
        )
        subprocess.run(["git", "mv", source.name, target.name], cwd=tmp_path, check=True)
        target.unlink()

        status = pr_manager._read_porcelain_status(tmp_path, git_timeout=10)
        paths = pr_manager._select_commit_paths(pr_manager._parse_porcelain_status(status), None)
        pr_manager._stage_commit_paths(paths, tmp_path, 10)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-status", "-z"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        ).stdout

        assert staged == b"D\x00source.txt\x00"


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
