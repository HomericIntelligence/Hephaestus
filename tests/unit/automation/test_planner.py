"""Tests for the Planner automation."""

import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import PlannerOptions
from hephaestus.automation.planner import Planner


@pytest.fixture
def mock_options() -> Any:
    """Create mock PlannerOptions."""
    return PlannerOptions(
        issues=[123],
        dry_run=False,
        force=False,
        parallel=1,
        system_prompt_file=None,
        skip_closed=True,
        enable_advise=True,
    )


@pytest.fixture
def planner(mock_options: Any) -> Any:
    """Create a Planner instance."""
    return Planner(mock_options)


class TestCallClaude:
    """Tests for _call_claude method.

    The planner delegates to
    :func:`hephaestus.automation.claude_invoke.invoke_claude_with_session`, so
    these tests patch ``subprocess.run`` in *that* module and exercise the
    full session-naming code path. Each test pins HEPH_TRUNK_GITHASH and
    isolates HOME so the session-file probe stays predictable.
    """

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: Any, tmp_path: Any) -> None:
        monkeypatch.setenv("HEPH_TRUNK_GITHASH", "abc1234")
        monkeypatch.setenv("HOME", str(tmp_path))

    def _patch_repo(self) -> Any:
        """Stub get_repo_root + get_repo_slug to deterministic values."""
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch("hephaestus.automation.planner_claude.get_repo_root", return_value=Path("/repo"))
        )
        stack.enter_context(
            patch("hephaestus.automation.planner_claude.get_repo_slug", return_value="TestRepo")
        )
        return stack

    def test_successful_call(self, planner: Any) -> None:
        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(stdout="This is a plan", returncode=0)

            result = planner._call_claude(
                "Test prompt",
                model="claude-opus-4-7",
                agent="planner",
                issue_number=123,
            )

            assert result == "This is a plan"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == "claude"
            # #1168: no transcript yet (HOME=tmp_path) → first call creates with
            # --session-id; later calls would --resume. Session id is model-keyed.
            assert "--session-id" in args
            assert "--name" in args
            assert "--resume" not in args
            assert "--model" in args
            assert "claude-opus-4-7" in args
            assert "--print" in args
            # Prompt is the last positional arg.
            assert args[-1] == "Test prompt"
            assert "--output-format" in args
            assert "text" in args

    def test_model_kwarg_pins_argv(self, planner: Any) -> None:
        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(stdout="ok", returncode=0)
            planner._call_claude("p", model="claude-haiku-4-5", agent="planner", issue_number=1)
            args = mock_run.call_args[0][0]
            assert "--model" in args
            i = args.index("--model")
            assert args[i + 1] == "claude-haiku-4-5"

    def test_empty_response(self, planner: Any) -> None:
        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(stdout="   ", returncode=0)

            with pytest.raises(RuntimeError, match="empty response"):
                planner._call_claude(
                    "Test prompt",
                    model="claude-opus-4-7",
                    agent="planner",
                    issue_number=1,
                )

    def test_timeout(self, planner: Any) -> None:
        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = subprocess.TimeoutExpired("claude", 300)

            with pytest.raises(RuntimeError, match="timed out"):
                planner._call_claude(
                    "Test prompt",
                    model="claude-opus-4-7",
                    agent="planner",
                    issue_number=1,
                    timeout=300,
                )

    def test_rate_limit_retry(self, planner: Any) -> None:
        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
            patch("hephaestus.automation.planner_claude.scan_quota_reset") as mock_scan,
        ):
            mock_run.side_effect = [
                subprocess.CalledProcessError(1, "claude", stderr="rate limit exceeded"),
                MagicMock(stdout="Success", returncode=0),
            ]
            mock_scan.side_effect = [0, None]
            with patch("time.sleep"):
                result = planner._call_claude(
                    "Test prompt",
                    model="claude-opus-4-7",
                    agent="planner",
                    issue_number=1,
                    max_retries=3,
                )
            assert result == "Success"
            assert mock_run.call_count == 2

    def test_claude_usage_cap_in_stdout_triggers_wait(self, planner: Any) -> None:
        usage_json = (
            '{"is_error": true, "api_error_status": 429, '
            '"result": "You\'re out of extra usage \xb7 resets May 8, 5pm '
            '(America/Los_Angeles)"}'
        )
        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
            patch("hephaestus.automation.planner_claude.wait_until") as mock_wait,
        ):
            mock_run.side_effect = [
                subprocess.CalledProcessError(1, "claude", output=usage_json, stderr=""),
                MagicMock(stdout="Success", returncode=0),
            ]
            result = planner._call_claude(
                "p",
                model="claude-opus-4-7",
                agent="planner",
                issue_number=1,
                max_retries=2,
            )

            assert result == "Success"
            assert mock_run.call_count == 2
            mock_wait.assert_called_once()
            (epoch,) = mock_wait.call_args[0]
            assert epoch > 0

    def test_system_prompt_passthrough(self, mock_options: Any, tmp_path: Any) -> None:
        sys_prompt = tmp_path / "system.md"
        sys_prompt.write_text("hi")
        mock_options.system_prompt_file = sys_prompt
        planner = Planner(mock_options)

        with (
            self._patch_repo(),
            patch("hephaestus.automation.claude_invoke.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(stdout="Response", returncode=0)
            planner._call_claude(
                "Test prompt",
                model="claude-opus-4-7",
                agent="planner",
                issue_number=1,
            )
            args = mock_run.call_args[0][0]
            assert "--system-prompt" in args
            assert str(sys_prompt) in args


class TestRunAdvise:
    """Tests for the planner's _run_advise (delegates to advise_runner).

    The Mnemosyne setup + prompt construction moved into the shared
    ``advise_runner`` module (#30), so these patch that module's symbols
    (``advise_runner.get_repo_root`` / ``advise_runner.ensure_mnemosyne`` /
    ``advise_runner.shutil``) rather than the planner's. The planner still
    supplies its own ``_call_claude`` as the invoker, so that patch stays.
    """

    def test_returns_findings(self, planner: Any) -> None:
        """Test successful advise returns findings."""
        with (
            patch("hephaestus.automation.advise_runner.get_repo_root") as mock_get_repo,
            patch.object(Path, "exists", return_value=True),
        ):
            mock_get_repo.return_value = Path("/repo")

            with patch.object(
                planner, "_call_claude", return_value="## Related Skills\nFound 3 skills"
            ):
                result = planner._run_advise(123, "Test Issue", "Issue body")

                assert "Related Skills" in result
                assert "Found 3 skills" in result

    def test_codex_advise_uses_codex_prompt_builder(self, mock_options: Any) -> None:
        """Codex runs should trigger the Codex `$advise` skill prompt."""
        mock_options.agent = "codex"
        planner = Planner(mock_options)

        with patch("hephaestus.automation.planner.run_advise", return_value="findings") as run:
            result = planner._run_advise(123, "Test Issue", "Issue body")

        assert result == "findings"
        assert run.call_args.kwargs["build_prompt"].__name__ == "get_codex_advise_prompt"

    def test_graceful_failure_on_error(self, planner: Any) -> None:
        """When advise errors, return a sentinel-comment (not silent ``""``).

        The silent-empty fallback became a marked-skipped breadcrumb so
        downstream readers can tell "advise failed" apart from "advise found
        nothing".
        """
        with patch("hephaestus.automation.advise_runner.get_repo_root") as mock_get_repo:
            mock_get_repo.side_effect = RuntimeError("Git error")

            result = planner._run_advise(123, "Test Issue", "Issue body")

            assert result.startswith("<!-- advise step skipped:")
            assert "Git error" in result

    def test_skips_when_mnemosyne_missing_and_clone_fails(self, planner: Any) -> None:
        """Returns sentinel-comment when ProjectMnemosyne is missing and clone fails."""
        with (
            patch("hephaestus.automation.advise_runner.get_repo_root") as mock_get_repo,
            patch(
                "hephaestus.automation.advise_runner.ensure_mnemosyne", return_value=False
            ) as mock_ensure,
        ):
            mock_get_repo.return_value = Path("/repo")

            with patch.object(Path, "exists", return_value=False):
                result = planner._run_advise(123, "Test Issue", "Issue body")

            assert result.startswith("<!-- advise step skipped:")
            assert "ProjectMnemosyne unavailable" in result
            mock_ensure.assert_called_once()

    def test_clones_mnemosyne_when_missing(self, planner: Any) -> None:
        """Test proceeds with advise after cloning ProjectMnemosyne."""
        call_count = [0]

        def patched_exists(p: Path) -> bool:
            call_count[0] += 1
            # First call: mnemosyne_root.exists() -> False (triggers ensure_mnemosyne)
            # Subsequent calls (marketplace check): True
            return call_count[0] != 1

        with (
            patch("hephaestus.automation.advise_runner.get_repo_root") as mock_get_repo,
            patch("hephaestus.automation.advise_runner.ensure_mnemosyne", return_value=True),
            patch.object(Path, "exists", patched_exists),
            patch.object(planner, "_call_claude", return_value="## Related Skills\nFound 2 skills"),
        ):
            mock_get_repo.return_value = Path("/repo")
            result = planner._run_advise(123, "Test Issue", "Issue body")

        assert "Related Skills" in result

    def test_marketplace_missing_triggers_reclone_and_succeeds(self, planner: Any) -> None:
        """Test that missing marketplace.json triggers re-clone and succeeds on retry."""
        # mnemosyne_root exists but marketplace.json is absent initially.
        # After re-clone, marketplace.json becomes present.
        exists_calls: list[Path] = []

        def patched_exists(p: Path) -> bool:
            exists_calls.append(p)
            # mnemosyne_root itself always "exists" (corrupt clone scenario)
            if p.name == "ProjectMnemosyne":
                return True
            # marketplace.json: absent on first check, present after re-clone
            if p.name == "marketplace.json":
                # First time called it's absent; from the second call it's present
                return exists_calls.count(p) > 1
            return True

        with (
            patch("hephaestus.automation.advise_runner.get_repo_root") as mock_get_repo,
            patch.object(Path, "exists", patched_exists),
            patch("hephaestus.automation.advise_runner.shutil.rmtree") as mock_rmtree,
            patch(
                "hephaestus.automation.advise_runner.ensure_mnemosyne", return_value=True
            ) as mock_ensure,
            patch.object(planner, "_call_claude", return_value="## Found Skills\nSkill A"),
        ):
            mock_get_repo.return_value = Path("/repo")
            result = planner._run_advise(123, "Test Issue", "Issue body")

        mock_rmtree.assert_called_once()
        mock_ensure.assert_called_once()
        assert "Found Skills" in result

    def test_marketplace_missing_reclone_fails_returns_sentinel(self, planner: Any) -> None:
        """Missing marketplace.json + failed re-clone returns the skipped sentinel."""

        def patched_exists(p: Path) -> bool:
            # mnemosyne_root exists but marketplace.json is always absent
            if p.name == "ProjectMnemosyne":
                return True
            return p.name != "marketplace.json"

        with (
            patch("hephaestus.automation.advise_runner.get_repo_root") as mock_get_repo,
            patch.object(Path, "exists", patched_exists),
            patch("hephaestus.automation.advise_runner.shutil.rmtree") as mock_rmtree,
            patch(
                "hephaestus.automation.advise_runner.ensure_mnemosyne", return_value=False
            ) as mock_ensure,
        ):
            mock_get_repo.return_value = Path("/repo")
            result = planner._run_advise(123, "Test Issue", "Issue body")

        mock_rmtree.assert_called_once()
        mock_ensure.assert_called_once()
        assert result.startswith("<!-- advise step skipped:")
        assert "marketplace.json missing" in result


class TestGeneratePlan:
    """Tests for _generate_plan method."""

    def test_plan_with_advise_findings(self, planner: Any) -> None:
        """Test plan generation with advise findings injected."""
        with (
            patch("hephaestus.automation.planner_review_loop.gh_issue_json") as mock_gh,
            patch.object(
                planner,
                "_run_advise",
                return_value="## Related Skills\nFound skills",
            ),
            patch.object(planner, "_call_claude", return_value="# Implementation Plan\nStep 1"),
        ):
            mock_gh.return_value = {
                "title": "Test Issue",
                "body": "Issue description",
            }

            plan = planner._generate_plan(123)

            assert "Implementation Plan" in plan
            # Verify _call_claude was called with context including advise findings
            call_args = planner._call_claude.call_args[0][0]
            assert "Prior Learnings" in call_args
            assert "Related Skills" in call_args

    def test_plan_without_advise(self, mock_options: Any) -> None:
        """Test plan generation with advise disabled."""
        mock_options.enable_advise = False
        planner = Planner(mock_options)

        with (
            patch("hephaestus.automation.planner_review_loop.gh_issue_json") as mock_gh,
            patch.object(planner, "_run_advise") as mock_advise,
            patch.object(planner, "_call_claude", return_value="# Implementation Plan\nStep 1"),
        ):
            mock_gh.return_value = {
                "title": "Test Issue",
                "body": "Issue description",
            }

            plan = planner._generate_plan(123)

            # Advise should not be called
            mock_advise.assert_not_called()

            # Plan should still be generated
            assert "Implementation Plan" in plan


class TestEnsureMnemosyne:
    """Tests for _ensure_mnemosyne method."""

    def test_clone_success(self, planner: Any, tmp_path: Any) -> None:
        """Test successful clone returns True and runs correct command."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "repo" in cmd
        assert "clone" in cmd
        assert "HomericIntelligence/ProjectMnemosyne" in cmd
        assert str(mnemosyne_root) in cmd

    def test_clone_failure(self, planner: Any, tmp_path: Any) -> None:
        """Test clone failure returns False and logs warning."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "gh", stderr="authentication failed"
            )

            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is False

    def test_no_clone_if_exists(self, planner: Any, tmp_path: Any) -> None:
        """Test does not clone when directory already exists (runs git pull instead)."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()

        with patch("subprocess.run") as mock_run:

            def run_existing(cmd: list[str], **_: object) -> MagicMock:
                if "rev-parse" in cmd:
                    return MagicMock(returncode=0, stdout="true\n", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_existing
            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is True
        # Should call git pull, not gh repo clone
        commands = [call.args[0] for call in mock_run.call_args_list]
        assert any("git" in cmd and "pull" in cmd for cmd in commands)
        assert not any("gh" in cmd and "clone" in cmd for cmd in commands)

    def test_lock_file_kept_after_successful_clone(self, planner: Any, tmp_path: Any) -> None:
        """Lock file must NOT be removed while fcntl.LOCK_EX is held (#370).

        Unlinking while holding the exclusive lock lets a second process open a
        new inode at the same path and acquire its own lock, silently breaking
        cross-process mutual exclusion.  The sentinel should persist; its
        presence after the clone is harmless.
        """
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        lock_path = tmp_path / ".mnemosyne.lock"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is True
        # After a successful clone the lock file fd is closed (LOCK_UN) but the
        # file itself remains on disk — it is a sentinel, not a temp file.
        assert lock_path.exists(), (
            "Lock file should remain on disk after clone (#370 — unlink-under-lock removed)"
        )

    def test_git_pull_called_when_directory_exists(self, planner: Any, tmp_path: Any) -> None:
        """Test that git pull --ff-only is called when directory already exists."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()

        with patch("subprocess.run") as mock_run:

            def run_existing(cmd: list[str], **_: object) -> MagicMock:
                if "rev-parse" in cmd:
                    return MagicMock(returncode=0, stdout="true\n", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_existing

            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is True
        commands = [call.args[0] for call in mock_run.call_args_list]
        cmd = next(cmd for cmd in commands if "pull" in cmd)
        assert "git" in cmd
        assert "-C" in cmd
        assert str(mnemosyne_root) in cmd
        assert "pull" in cmd
        assert "--ff-only" in cmd

    def test_git_pull_failure_logs_warning_and_returns_true(
        self, planner: Any, tmp_path: Any
    ) -> None:
        """Test that a git pull failure logs a warning but still returns True."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()

        with patch("subprocess.run") as mock_run:

            def run_existing(cmd: list[str], **_: object) -> MagicMock:
                if "rev-parse" in cmd:
                    return MagicMock(returncode=0, stdout="true\n", stderr="")
                raise subprocess.CalledProcessError(1, "git", stderr="not a fast-forward")

            mock_run.side_effect = run_existing

            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is True

    def test_concurrent_clone_only_once(self, mock_options: Any, tmp_path: Any) -> None:
        """Test concurrent calls only clone once (lock prevents double-clone)."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"

        clone_calls = []
        start_event = threading.Event()

        def fake_subprocess(cmd: list[str], **kwargs: object) -> MagicMock:
            if "rev-parse" in cmd:
                return MagicMock(returncode=0, stdout="true\n", stderr="")
            # Only count gh repo clone calls, not git pull calls
            if "gh" in cmd and "clone" in cmd:
                clone_calls.append(1)
                # Create the directory so subsequent checks see it
                mnemosyne_root.mkdir(exist_ok=True)
            return MagicMock(returncode=0)

        planner1 = Planner(mock_options)
        planner2 = Planner(mock_options)

        results: list[bool] = []

        def worker(p: Planner) -> None:
            start_event.wait()
            results.append(p._ensure_mnemosyne(mnemosyne_root))

        t1 = threading.Thread(target=worker, args=(planner1,))
        t2 = threading.Thread(target=worker, args=(planner2,))

        with patch("subprocess.run", side_effect=fake_subprocess):
            t1.start()
            t2.start()
            start_event.set()
            t1.join()
            t2.join()

        assert all(results), "Both threads should return True"
        assert len(clone_calls) == 1, "Clone should only happen once"

    def test_clone_timeout_returns_false(self, planner: Any, tmp_path: Any) -> None:
        """TimeoutExpired on gh repo clone must return False without raising (#368)."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("gh", 120)

            result = planner._ensure_mnemosyne(mnemosyne_root)

        assert result is False

    def test_clone_call_has_timeout_parameter(self, planner: Any, tmp_path: Any) -> None:
        """Clone call must have a timeout= argument to prevent indefinite blocking (#368).

        A missing timeout causes the call to block indefinitely while holding
        both the threading.Lock and fcntl.LOCK_EX, starving all parallel workers.
        """
        mnemosyne_root = tmp_path / "ProjectMnemosyne"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            planner._ensure_mnemosyne(mnemosyne_root)

        call_kwargs = mock_run.call_args.kwargs
        assert "timeout" in call_kwargs, "subprocess.run for clone must pass timeout="
        assert call_kwargs["timeout"] == 120


class TestPrCoverageSkip:
    """FM1 idempotency guard: skip planning issues already covered by a PR.

    Regression for the 2026-06-15 loop run that re-planned #1357/#1289/#1179
    while their PRs were open (and again after PR #1358 merged but the issue
    stayed OPEN), burning ~5.5h on duplicate/zombie PRs (#1359, #1365).
    """

    def test_skips_when_open_pr_covers_issue(self, planner: Any) -> None:
        """find_pr_for_issue returns a PR → skip, no agent call, no plan post."""
        with (
            patch(
                "hephaestus.automation.planner.find_merged_closing_pr",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.planner.find_pr_for_issue",
                return_value=42,
            ),
            patch.object(planner, "_run_plan_review_loop") as mock_loop,
            patch.object(planner, "_post_plan") as mock_post,
            patch.object(planner, "_call_claude") as mock_claude,
        ):
            result = planner._plan_issue(123)

        assert result.success is True
        assert result.plan_already_exists is True
        mock_loop.assert_not_called()
        mock_post.assert_not_called()
        mock_claude.assert_not_called()

    def test_closes_issue_when_merged_pr_covers_and_skips(self, planner: Any) -> None:
        """Merged closing PR + open issue → close issue, skip, no re-plan."""
        with (
            patch(
                "hephaestus.automation.planner.find_merged_closing_pr",
                return_value=1358,
            ),
            patch(
                "hephaestus.automation.planner.close_issue_as_covered",
            ) as mock_close,
            patch(
                "hephaestus.automation.planner.find_pr_for_issue",
            ) as mock_open_pr,
            patch.object(planner, "_run_plan_review_loop") as mock_loop,
            patch.object(planner, "_post_plan") as mock_post,
            patch.object(planner, "_call_claude") as mock_claude,
        ):
            result = planner._plan_issue(1357)

        assert result.success is True
        assert result.plan_already_exists is True
        mock_close.assert_called_once_with(1357, 1358)
        # Merged-PR gate short-circuits before the open-PR lookup.
        mock_open_pr.assert_not_called()
        mock_loop.assert_not_called()
        mock_post.assert_not_called()
        mock_claude.assert_not_called()

    def test_plans_when_no_pr_covers_issue(self, planner: Any) -> None:
        """No open/merged PR → planning proceeds normally."""
        with (
            patch(
                "hephaestus.automation.planner.find_merged_closing_pr",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.planner.find_pr_for_issue",
                return_value=None,
            ),
            patch.object(planner, "_has_existing_plan", return_value=False),
            patch.object(
                planner,
                "_run_plan_review_loop",
                return_value=("# Plan", "GO review", 1, True),
            ),
            patch.object(planner, "_post_plan") as mock_post,
        ):
            result = planner._plan_issue(123)

        assert result.success is True
        assert result.plan_already_exists is False
        mock_post.assert_called_once()
