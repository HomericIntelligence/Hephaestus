"""Unit tests for hephaestus.github.tidy — focusing on parse_problem_branches and timeouts."""

import argparse
import asyncio
import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.tidy import (
    _detect_default_branch,
    _in_git_repo,
    _repo_root,
    _working_tree_clean,
    parse_problem_branches,
)

tidy_module = importlib.import_module("hephaestus.github.tidy")


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

    def test_run_tidy_and_find_problem_branches_parses_even_after_gh_tidy_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gh-tidy failures still return parseable problem branches."""
        monkeypatch.setattr(tidy_module, "_run_gh_tidy", lambda trunk, dry_run: (2, ONE_PROBLEM))

        assert tidy_module._run_tidy_and_find_problem_branches("main", False) == [
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
        with patch("hephaestus.github.tidy.run_subprocess", create=True) as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(["git"], 10)
            with pytest.raises(subprocess.TimeoutExpired):
                _working_tree_clean()

    def test_working_tree_clean_uses_run_subprocess(self) -> None:
        """_working_tree_clean routes git status through run_subprocess."""
        with patch("hephaestus.github.tidy.run_subprocess", create=True) as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            _working_tree_clean()
            mock_run.assert_called_once_with(
                ["git", "status", "--porcelain"],
                check=False,
                timeout=tidy_module.METADATA_TIMEOUT,
            )

    def test_in_git_repo_with_timeout(self) -> None:
        """_in_git_repo propagates TimeoutExpired."""
        with patch("hephaestus.github.tidy.run_subprocess", create=True) as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(["git"], 10)
            with pytest.raises(subprocess.TimeoutExpired):
                _in_git_repo()

    def test_in_git_repo_uses_run_subprocess(self) -> None:
        """_in_git_repo routes git rev-parse through run_subprocess."""
        with patch("hephaestus.github.tidy.run_subprocess", create=True) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _in_git_repo()
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--git-dir"],
                check=False,
                timeout=tidy_module.METADATA_TIMEOUT,
            )

    def test_repo_root_uses_run_subprocess(self) -> None:
        """_repo_root routes git root detection through run_subprocess."""
        with patch("hephaestus.github.tidy.run_subprocess", create=True) as mock_run:
            mock_run.return_value = MagicMock(stdout="/path/to/repo\n")
            _repo_root()
            mock_run.assert_called_once_with(
                ["git", "rev-parse", "--show-toplevel"],
                timeout=tidy_module.METADATA_TIMEOUT,
            )

    def test_direct_rebase_agent_uses_env_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct tidy conflict agents honor HEPH_AGENT_REBASE_TIMEOUT (#1417)."""
        monkeypatch.setenv("HEPH_AGENT_REBASE_TIMEOUT", "1234")
        with patch("hephaestus.github.tidy.run_agent_text") as run_agent:
            run_agent.return_value = MagicMock(stdout="rebased")

            tidy_module._run_direct_rebase_agent("codex", "prompt", "feature/a", Path("/repo"))

        assert run_agent.call_args.kwargs["timeout"] == 1234

    def test_direct_rebase_agent_default_timeout(self) -> None:
        """Default rebase-agent timeout is AGENT_REBASE_TIMEOUT (2400)."""
        with patch("hephaestus.github.tidy.run_agent_text") as run_agent:
            run_agent.return_value = MagicMock(stdout="rebased")

            tidy_module._run_direct_rebase_agent("codex", "prompt", "feature/a", Path("/repo"))

        assert run_agent.call_args.kwargs["timeout"] == 2400
