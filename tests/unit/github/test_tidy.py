"""Unit tests for hephaestus.github.tidy — focusing on parse_problem_branches and timeouts."""

import argparse
import asyncio
import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from hephaestus.github.tidy import (
    _detect_default_branch,
    _in_git_repo,
    _repo_root,
    _working_tree_clean,
    parse_problem_branches,
)

tidy_module = importlib.import_module("hephaestus.github.tidy")

WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123-finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "",
        "worktree /repo/.worktrees/topic",
        "HEAD 789abc",
        "branch refs/heads/topic",
        "",
        "worktree /repo/.worktrees/detached",
        "HEAD deadbeef",
        "detached",
        "",
        "bare",
        "",
    )
)

SPACED_WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123 finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "",
    )
)

LOCKED_SPACED_WORKTREE_PORCELAIN = "\0".join(
    (
        "worktree /repo",
        "HEAD abcdef",
        "branch refs/heads/main",
        "",
        "worktree /repo/.worktrees/123 finished",
        "HEAD 123456",
        "branch refs/heads/123-finished",
        "locked",
        "",
    )
)


def test_tidy_swarm_model_matches_canonical_sonnet() -> None:
    """Drift guard: the tidy swarm model mirrors claude_models.SONNET.

    ``hephaestus.github`` must not import ``hephaestus.automation`` (layering
    boundary, see test_no_import_cycles), so tidy keeps a local model constant.
    This test — which lives outside that boundary — pins the two together so a
    canonical model bump doesn't silently leave tidy behind.
    """
    from hephaestus.automation.claude_models import SONNET
    from hephaestus.github.tidy import _TIDY_SWARM_MODEL

    assert _TIDY_SWARM_MODEL == SONNET


# Fixture: clean gh-tidy run (no problem branches)
CLEAN_OUTPUT = """\
Checking out main and pulling the latest from remote origin...
Finished tidying!
"""

# Fixture: one problem branch
ONE_PROBLEM = """\
Rebasing ALL local branches on to latest master...
Rebasing feature/my-branch...
WARNING: Problem rebasing feature/my-branch
Finished rebasing!

Cleaning unnecessary files & optimizing your local repo...
WARNING: Unable to auto-rebase the following branches:
    * feature/my-branch

Finished tidying!
"""

# Fixture: multiple problem branches
MULTI_PROBLEM = """\
WARNING: Unable to auto-rebase the following branches:
    * feature/alpha
    * fix/beta-crash
    * chore/deps-update

Finished tidying!
"""

# Fixture: ANSI-coloured output (gh-tidy emits \e[93m yellow for warnings)
ANSI_PROBLEM = (
    "\x1b[93mWARNING: Unable to auto-rebase the following branches:\x1b[0m\n"
    "\x1b[93m    * feature/with-ansi\x1b[0m\n"
    "\x1b[92mFinished tidying!\x1b[0m\n"
)

# Fixture: problem header with no bullets (edge case — header present, no branch listed)
EMPTY_PROBLEM_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:

Finished tidying!
"""

# Fixture: problem header where a non-bullet line immediately follows
TRAILING_TEXT_AFTER_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:
    * chore/broken
Please fix manually.
Finished tidying!
"""


def test_clean_output_returns_empty() -> None:
    """No problem branches when output is a clean run."""
    assert parse_problem_branches(CLEAN_OUTPUT) == []


def test_single_problem_branch() -> None:
    """Single problem branch is extracted correctly."""
    result = parse_problem_branches(ONE_PROBLEM)
    assert result == ["feature/my-branch"]


def test_multiple_problem_branches() -> None:
    """All branches listed under the warning header are returned."""
    result = parse_problem_branches(MULTI_PROBLEM)
    assert result == ["feature/alpha", "fix/beta-crash", "chore/deps-update"]


def test_ansi_codes_stripped() -> None:
    """ANSI escape sequences are stripped before parsing."""
    result = parse_problem_branches(ANSI_PROBLEM)
    assert result == ["feature/with-ansi"]


def test_empty_problem_block() -> None:
    """Warning header with no bullet lines returns empty list."""
    result = parse_problem_branches(EMPTY_PROBLEM_BLOCK)
    assert result == []


def test_trailing_text_terminates_block() -> None:
    """Non-bullet line after the branch list terminates parsing."""
    result = parse_problem_branches(TRAILING_TEXT_AFTER_BLOCK)
    assert result == ["chore/broken"]


def test_no_problem_header_at_all() -> None:
    """Output with no warning header returns empty list."""
    result = parse_problem_branches("Finished tidying!\n")
    assert result == []


def test_run_gh_tidy_rebases_and_auto_deletes_merged_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gh-tidy boundary uses the complete unattended cleanup argv."""
    process = MagicMock()
    process.stdout = iter(())
    process.returncode = 0
    popen = MagicMock()
    popen.return_value.__enter__.return_value = process
    monkeypatch.setattr(tidy_module.subprocess, "Popen", popen)

    assert tidy_module._run_gh_tidy("main", dry_run=False) == (0, "")

    popen.assert_called_once_with(
        [
            "gh",
            "tidy",
            "--rebase-all",
            "--auto-delete-merged",
            "--trunk",
            "main",
            "--skip-gc",
        ],
        stdin=tidy_module.sys.stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=ANY,
    )
    process.wait.assert_called_once_with()


def test_parse_worktree_porcelain_skips_main_and_detached_worktrees() -> None:
    """Cleanup candidates require a non-main worktree with an attached branch."""
    assert hasattr(tidy_module, "_parse_worktree_porcelain")
    assert tidy_module._parse_worktree_porcelain(WORKTREE_PORCELAIN, Path("/repo")) == [
        (Path("/repo/.worktrees/123-finished"), "123-finished"),
        (Path("/repo/.worktrees/topic"), "topic"),
    ]


def test_parse_worktree_porcelain_skips_primary_from_linked_worktree() -> None:
    """The primary worktree is never a cleanup candidate from a linked worktree."""
    assert tidy_module._parse_worktree_porcelain(
        WORKTREE_PORCELAIN,
        Path("/repo/.worktrees/topic"),
    ) == [(Path("/repo/.worktrees/123-finished"), "123-finished")]


def test_worktree_porcelain_requests_nul_terminated_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree discovery requests Git's delimiter-safe porcelain format."""
    result = subprocess.CompletedProcess([], 0, stdout="inventory", stderr="")
    run_git = MagicMock(return_value=result)
    monkeypatch.setattr(tidy_module, "run_git", run_git)

    assert tidy_module._worktree_porcelain() == "inventory"
    run_git.assert_called_once_with(["worktree", "list", "--porcelain", "-z"])


def test_parse_worktree_porcelain_preserves_space_in_path() -> None:
    """A worktree path containing spaces remains paired with its branch."""
    assert tidy_module._parse_worktree_porcelain(
        SPACED_WORKTREE_PORCELAIN,
        Path("/repo"),
    ) == [(Path("/repo/.worktrees/123 finished"), "123-finished")]


def test_cleanup_stale_worktrees_dry_run_reports_closed_issue_without_removing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dry-run reports a closed-issue worktree and never invokes git removal."""
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    if not hasattr(tidy_module, "_cleanup_stale_worktrees"):
        pytest.fail("tidy does not yet implement stale-worktree cleanup")
    monkeypatch.setattr(tidy_module, "_worktree_porcelain", lambda: WORKTREE_PORCELAIN)
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert hasattr(tidy_module, "_cleanup_stale_worktrees")
    assert tidy_module._cleanup_stale_worktrees(tmp_path, "main", dry_run=True) == 0
    assert "Would remove stale worktree" in caplog.text
    remove.assert_not_called()


def test_cleanup_stale_worktree_with_space_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dry-run evaluates and reports the complete space-containing path."""
    spaced_path = Path("/repo/.worktrees/123 finished")
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: SPACED_WORKTREE_PORCELAIN,
    )
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    is_dirty = MagicMock(return_value=False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", is_dirty)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=True) == 0
    is_dirty.assert_called_once_with(spaced_path)
    assert str(spaced_path) in caplog.text
    assert "123-finished" in caplog.text
    remove.assert_not_called()


def test_cleanup_skips_locked_worktree_with_space_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A locked spaced-path worktree is skipped without removal."""
    spaced_path = Path("/repo/.worktrees/123 finished")
    caplog.set_level("INFO", logger="hephaestus.github.tidy")
    monkeypatch.setattr(
        tidy_module,
        "_worktree_porcelain",
        lambda: LOCKED_SPACED_WORKTREE_PORCELAIN,
    )
    monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda issue: issue == 123)
    monkeypatch.setattr(tidy_module, "_branch_is_merged", lambda branch, trunk: False)
    monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda path: False)
    remove = MagicMock()
    monkeypatch.setattr(tidy_module, "_remove_worktree", remove)

    assert tidy_module._cleanup_stale_worktrees(Path("/repo"), "main", dry_run=False) == 0
    assert f"Skipping locked worktree {spaced_path}" in caplog.text
    remove.assert_not_called()


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "feature/foo-bar",
        "fix/issue-123",
        "chore/bump-deps",
        "release/v2.0.0",
    ],
)
def test_various_branch_name_formats(branch: str) -> None:
    """Branch names with slashes, numbers, and hyphens are all parsed correctly."""
    output = (
        "WARNING: Unable to auto-rebase the following branches:\n"
        f"    * {branch}\n"
        "Finished tidying!\n"
    )
    assert parse_problem_branches(output) == [branch]


def test_dispatch_swarm_runs_codex_agents_in_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex swarm dispatch should preserve max_concurrent semantics."""
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def fake_to_thread(func: object, *args: object) -> str:
        calls.append((func, args))
        return "fixed"

    monkeypatch.setattr(tidy_module.asyncio, "to_thread", fake_to_thread)

    result = asyncio.run(
        tidy_module._dispatch_swarm(
            ["feature/a"],
            "main",
            tmp_path,
            "owner/repo",
            max_concurrent=1,
            dry_run=False,
            agent="codex",
        )
    )

    assert result == {"feature/a": "fixed"}
    assert calls
    assert calls[0][0] is tidy_module._run_direct_rebase_agent
    assert calls[0][1][0] == "codex"


class TestTidyHandlers:
    """Tests for extracted tidy workflow handlers."""

    def test_run_tidy_and_find_problem_branches_fails_closed_on_gh_tidy_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero gh tidy exit must raise, never fabricate a clean result.

        Athena #103: parsing partial output after gh tidy exited non-zero and
        then claiming "All branches rebased cleanly" produced a false success.
        The function now fails closed so callers cannot lie about cleanup state.
        """
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry_run: (128, ONE_PROBLEM))

        with pytest.raises(tidy_module.TidyExecutionError) as excinfo:
            tidy_module._run_tidy_and_find_problem_branches("main", False)
        assert excinfo.value.exit_code == 128

        # dry-run never mutates state, so it may still parse output for preview.
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry_run: (128, ONE_PROBLEM))
        assert tidy_module._run_tidy_and_find_problem_branches("main", True) == [
            "feature/my-branch"
        ]

    def test_handle_problem_branches_dry_run_json(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Dry-run problem branches emit the existing ok JSON envelope."""
        args = argparse.Namespace(no_swarm=False, dry_run=True, json=True, max_concurrent=5)

        assert (
            tidy_module._handle_problem_branches(
                args,
                ["feature/a"],
                "main",
                tmp_path,
                "owner/repo",
                "claude",
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["problem_branches"] == ["feature/a"]


class TestMain:
    """Smoke tests for hephaestus.github.tidy.main() covering --json branches."""

    def test_cleanup_from_linked_worktree_never_targets_primary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleanup invoked from a linked worktree excludes the primary checkout."""
        repo = tmp_path / "repo"
        linked_root = tmp_path / "topic-linked"
        stale_root = tmp_path / "123-finished"

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "--initial-branch=main", str(repo))
        git("-C", str(repo), "config", "user.name", "Test User")
        git("-C", str(repo), "config", "user.email", "test@example.com")
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git("-C", str(repo), "add", "tracked.txt")
        git("-C", str(repo), "commit", "-m", "test fixture")
        git("-C", str(repo), "worktree", "add", "-b", "topic", str(linked_root))
        git("-C", str(repo), "worktree", "add", "-b", "123-finished", str(stale_root))

        monkeypatch.chdir(linked_root)
        monkeypatch.setattr(tidy_module, "detect_repo_from_remote", lambda: "owner/repo")
        monkeypatch.setattr(tidy_module, "_working_tree_clean", lambda: True)
        monkeypatch.setattr(tidy_module, "_in_git_repo", lambda: True)
        monkeypatch.setattr(tidy_module, "_worktree_is_dirty", lambda _path: False)
        monkeypatch.setattr(tidy_module, "_issue_is_closed", lambda _issue: False)
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        branch_is_merged = MagicMock(return_value=True)
        monkeypatch.setattr(tidy_module, "_branch_is_merged", branch_is_merged)
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-tidy",
                "--cleanup-stale-worktrees",
                "--dry-run",
                "--agent",
                "claude",
            ],
        )

        assert tidy_module.main() == 0
        assert [call.args[0] for call in branch_is_merged.call_args_list] == ["123-finished"]

    def test_env_validation_failure_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When env validation fails, --json emits an error envelope."""
        import json

        monkeypatch.setattr(tidy_module, "_validate_environment", lambda: None)
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--json", "--agent", "claude"])
        assert tidy_module.main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "environment" in payload["message"]

    def test_no_problem_branches_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Clean tidy run with --json emits ok envelope."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: [])
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--json", "--agent", "claude"])
        assert tidy_module.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["problem_branches"] == 0

    def test_no_swarm_with_problems_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """--no-swarm with problem branches emits error envelope and exits 1."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: ["feature/a"])
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-tidy", "--json", "--no-swarm", "--agent", "claude"]
        )
        assert tidy_module.main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["swarm"] == "skipped"

    def test_handle_problem_branches_no_swarm_json(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Extracted problem-branch handler emits the existing no-swarm JSON."""
        import json

        args = tidy_module._build_arg_parser().parse_args(
            ["--json", "--no-swarm", "--agent", "claude"]
        )

        assert (
            tidy_module._handle_tidy_problem_branches(
                args=args,
                agent="claude",
                problem_branches=["feature/a"],
                trunk="main",
                repo_path=tmp_path,
                repo_slug="owner/repo",
            )
            == 1
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["problem_branches"] == ["feature/a"]
        assert payload["swarm"] == "skipped"

    def test_dry_run_with_problems_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """--dry-run with problem branches emits ok envelope."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: ["feature/a"])
        monkeypatch.setattr(
            "sys.argv", ["hephaestus-tidy", "--json", "--dry-run", "--agent", "claude"]
        )
        assert tidy_module.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["dry_run"] is True

    def test_full_dispatch_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """End-to-end with swarm dispatch (mocked) emits results envelope."""
        import json

        monkeypatch.setattr(
            tidy_module, "_validate_environment", lambda: ("owner/repo", "", tmp_path)
        )
        monkeypatch.setattr(tidy_module, "_detect_default_branch", lambda _x: "main")
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry: (0, ""))
        monkeypatch.setattr(tidy_module, "parse_problem_branches", lambda _o: ["feature/a"])

        async def fake_dispatch(*args, **kwargs):
            return {"feature/a": "rebased"}

        monkeypatch.setattr(tidy_module, "_dispatch_swarm", fake_dispatch)
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--json", "--agent", "claude"])
        assert tidy_module.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["results"] == {"feature/a": "rebased"}

    def test_env_validation_failure_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without --json, env-validation failure still exits 1."""
        monkeypatch.setattr(tidy_module, "_validate_environment", lambda: None)
        monkeypatch.setattr("sys.argv", ["hephaestus-tidy", "--agent", "claude"])
        assert tidy_module.main() == 1

    def test_main_threads_explicit_pi_policy_to_agent_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider resolution receives the CLI-owned Pi policy and auth budget."""
        captured: dict[str, object] = {}

        def fake_resolve(
            agent,
            *,
            disable_pi_automation,
            auth_status_timeout,
            pi_isolation_adapter,
            pi_dir,
        ):
            captured.update(
                agent=agent,
                disable_pi_automation=disable_pi_automation,
                auth_status_timeout=auth_status_timeout,
                pi_isolation_adapter=pi_isolation_adapter,
                pi_dir=pi_dir,
            )
            return "codex"

        monkeypatch.setattr(tidy_module, "resolve_agent", fake_resolve)
        monkeypatch.setattr(tidy_module, "_validate_environment", lambda: None)
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-tidy",
                "--agent",
                "codex",
                "--disable-pi-automation",
                "--auth-status-timeout",
                "19",
            ],
        )

        assert tidy_module.main() == 1
        assert captured == {
            "agent": "codex",
            "disable_pi_automation": True,
            "auth_status_timeout": 19,
            "pi_isolation_adapter": None,
            "pi_dir": None,
        }


class TestTimeoutHandling:
    """Tests for subprocess timeout handling in tidy helpers."""

    def test_detect_default_branch_with_timeout(self) -> None:
        """_detect_default_branch falls back to 'main' on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(["gh"], 120)
            result = _detect_default_branch(None)
            assert result == "main"

    def test_detect_default_branch_calls_with_network_timeout(self) -> None:
        """_detect_default_branch passes a positive timeout through gh_call.

        _detect_default_branch now routes through
        :func:`hephaestus.github.client.gh_call`, which invokes the subprocess
        via ``run_subprocess`` with ``timeout=gh_cli_timeout()`` (#713). Assert
        at that seam that a positive timeout is still supplied, preserving the
        no-timeout-less-read invariant after the adapter move.
        """
        with patch("hephaestus.github.client.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(stdout="main\n")
            _detect_default_branch(None)
            # Verify the call included a positive timeout
            assert mock_run.called
            call_kwargs = mock_run.call_args[1]
            assert "timeout" in call_kwargs
            assert call_kwargs["timeout"] > 0

    def test_working_tree_clean_with_timeout(self) -> None:
        """_working_tree_clean propagates TimeoutExpired."""
        with patch("hephaestus.github.tidy._shared_working_tree_clean") as shared_clean:
            shared_clean.side_effect = subprocess.TimeoutExpired(["git"], 10)
            with pytest.raises(subprocess.TimeoutExpired):
                _working_tree_clean()

    def test_working_tree_clean_uses_shared_helper(self) -> None:
        """_working_tree_clean routes git status through the shared git helper."""
        with patch("hephaestus.github.tidy._shared_working_tree_clean") as shared_clean:
            shared_clean.return_value = True
            assert _working_tree_clean() is True
            shared_clean.assert_called_once_with()

    def test_in_git_repo_with_timeout(self) -> None:
        """_in_git_repo propagates TimeoutExpired."""
        with patch("hephaestus.github.tidy._shared_in_git_repo") as shared_in_repo:
            shared_in_repo.side_effect = subprocess.TimeoutExpired(["git"], 10)
            with pytest.raises(subprocess.TimeoutExpired):
                _in_git_repo()

    def test_in_git_repo_uses_shared_helper(self) -> None:
        """_in_git_repo routes git rev-parse through the shared git helper."""
        with patch("hephaestus.github.tidy._shared_in_git_repo") as shared_in_repo:
            shared_in_repo.return_value = True
            assert _in_git_repo() is True
            shared_in_repo.assert_called_once_with()

    def test_repo_root_uses_shared_helper(self) -> None:
        """_repo_root routes git root detection through the shared git helper."""
        with patch("hephaestus.github.tidy._shared_repo_root") as shared_repo_root:
            shared_repo_root.return_value = Path("/path/to/repo")
            assert _repo_root() == Path("/path/to/repo")
            shared_repo_root.assert_called_once_with()

    def test_direct_rebase_agent_uses_explicit_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Removed HEPH_AGENT_REBASE_TIMEOUT is inert; explicit timeout wins."""
        monkeypatch.setenv("HEPH_AGENT_REBASE_TIMEOUT", "1234")
        with patch("hephaestus.github.tidy.run_agent_text") as run_agent:
            run_agent.return_value = MagicMock(stdout="rebased")

            tidy_module._run_direct_rebase_agent(
                "codex", "prompt", "feature/a", Path("/repo"), timeout=37
            )

        assert run_agent.call_args.kwargs["timeout"] == 37
        assert run_agent.call_args.kwargs["model"] == tidy_module._TIDY_SWARM_MODEL

    def test_direct_rebase_agent_default_timeout(self) -> None:
        """Default rebase-agent timeout is AGENT_REBASE_TIMEOUT (2400)."""
        with patch("hephaestus.github.tidy.run_agent_text") as run_agent:
            run_agent.return_value = MagicMock(stdout="rebased")

            tidy_module._run_direct_rebase_agent("codex", "prompt", "feature/a", Path("/repo"))

        assert run_agent.call_args.kwargs["timeout"] == 2400


def test_tidy_parser_accepts_explicit_log_format() -> None:
    """Tidy logging is selected explicitly and remains separate from --json."""
    args = tidy_module._build_arg_parser().parse_args(["--log-format", "json"])

    assert args.log_format == "json"
    assert args.json is False


def test_tidy_configure_logging_forwards_explicit_format() -> None:
    """The tidy adapter forwards the selected format to shared CLI logging."""
    with patch.object(tidy_module, "configure_cli_logging") as configure:
        tidy_module._configure_logging(True, "json")

    configure.assert_called_once_with(verbose=True, log_format="json")
