"""Unit tests for hephaestus.github.fleet_sync — pure logic functions and timeouts."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.fleet_sync import PRInfo, PRStatus, _ci_state, get_resign_email

fleet_sync_module = importlib.import_module("hephaestus.github.fleet_sync")


class TestCiState:
    """Tests for _ci_state() aggregation logic."""

    def test_empty_checks_returns_unknown(self) -> None:
        """Empty check list produces UNKNOWN state."""
        assert _ci_state([]) == "UNKNOWN"

    def test_all_success_returns_success(self) -> None:
        """All successful checks produce SUCCESS state."""
        checks = [
            {"conclusion": "SUCCESS", "state": "SUCCESS"},
            {"conclusion": "SUCCESS", "state": "SUCCESS"},
        ]
        assert _ci_state(checks) == "SUCCESS"

    def test_any_failure_returns_failure(self) -> None:
        """Any failed check causes FAILURE state."""
        checks = [
            {"conclusion": "SUCCESS", "state": "SUCCESS"},
            {"conclusion": "FAILURE", "state": "FAILURE"},
        ]
        assert _ci_state(checks) == "FAILURE"

    def test_any_pending_without_failure_returns_pending(self) -> None:
        """Pending check without failures returns PENDING."""
        checks = [
            {"conclusion": "SUCCESS", "state": "SUCCESS"},
            {"conclusion": "PENDING", "state": "PENDING"},
        ]
        assert _ci_state(checks) == "PENDING"

    def test_none_conclusion_returns_pending(self) -> None:
        """Check with None conclusion (still running) returns PENDING."""
        checks = [{"conclusion": None, "state": "QUEUED"}]
        assert _ci_state(checks) == "PENDING"

    def test_timed_out_is_failure(self) -> None:
        """TIMED_OUT conclusion maps to FAILURE state."""
        checks = [{"conclusion": "TIMED_OUT", "state": "COMPLETED"}]
        assert _ci_state(checks) == "FAILURE"

    def test_cancelled_is_failure(self) -> None:
        """CANCELLED conclusion maps to FAILURE state."""
        checks = [{"conclusion": "CANCELLED", "state": "COMPLETED"}]
        assert _ci_state(checks) == "FAILURE"

    def test_action_required_is_failure(self) -> None:
        """ACTION_REQUIRED conclusion maps to FAILURE state."""
        checks = [{"conclusion": "ACTION_REQUIRED", "state": "COMPLETED"}]
        assert _ci_state(checks) == "FAILURE"

    def test_lowercase_failure_state(self) -> None:
        """Lowercase 'failure' state (from some API responses) maps to FAILURE."""
        checks = [{"conclusion": "failure", "state": "failure"}]
        assert _ci_state(checks) == "FAILURE"

    def test_in_progress_is_pending(self) -> None:
        """IN_PROGRESS conclusion maps to PENDING state."""
        checks = [{"conclusion": "IN_PROGRESS", "state": "IN_PROGRESS"}]
        assert _ci_state(checks) == "PENDING"

    def test_failure_takes_priority_over_pending(self) -> None:
        """FAILURE takes priority over PENDING when both present."""
        checks = [
            {"conclusion": "FAILURE", "state": "COMPLETED"},
            {"conclusion": "PENDING", "state": "QUEUED"},
        ]
        assert _ci_state(checks) == "FAILURE"


class TestPRStatus:
    """Tests for PRStatus enum values."""

    def test_all_statuses_defined(self) -> None:
        """All expected PR status values are accessible."""
        assert PRStatus.READY is not None
        assert PRStatus.OUTDATED is not None
        assert PRStatus.CONFLICTED is not None
        assert PRStatus.FAILING is not None
        assert PRStatus.UNKNOWN is not None

    def test_statuses_are_distinct(self) -> None:
        """All PR status values are distinct."""
        statuses = [
            PRStatus.READY,
            PRStatus.OUTDATED,
            PRStatus.CONFLICTED,
            PRStatus.FAILING,
            PRStatus.UNKNOWN,
        ]
        assert len(set(statuses)) == len(statuses)


class TestPRInfo:
    """Tests for PRInfo dataclass construction."""

    def test_construct_with_required_fields(self) -> None:
        """PRInfo can be constructed with all required fields."""
        pr = PRInfo(
            repo="MyRepo",
            number=42,
            title="feat: add something",
            head_ref="feat/add-something",
            base_ref="main",
            head_sha="abc123",
            mergeable="MERGEABLE",
            merge_state="CLEAN",
            ci_state="SUCCESS",
        )
        assert pr.repo == "MyRepo"
        assert pr.number == 42
        assert pr.status == PRStatus.UNKNOWN
        assert pr.conflict_files == []

    def test_construct_with_custom_status(self) -> None:
        """PRInfo status field can be overridden."""
        pr = PRInfo(
            repo="MyRepo",
            number=1,
            title="fix: something",
            head_ref="fix/something",
            base_ref="main",
            head_sha="deadbeef",
            mergeable="MERGEABLE",
            merge_state="CLEAN",
            ci_state="SUCCESS",
            status=PRStatus.READY,
        )
        assert pr.status == PRStatus.READY

    def test_conflict_files_default_is_empty_list(self) -> None:
        """conflict_files defaults to an empty list (not shared mutable default)."""
        pr1 = PRInfo(
            repo="R",
            number=1,
            title="t",
            head_ref="h",
            base_ref="b",
            head_sha="s",
            mergeable="M",
            merge_state="C",
            ci_state="S",
        )
        pr2 = PRInfo(
            repo="R",
            number=2,
            title="t",
            head_ref="h",
            base_ref="b",
            head_sha="s",
            mergeable="M",
            merge_state="C",
            ci_state="S",
        )
        pr1.conflict_files.append("file.txt")
        assert pr2.conflict_files == [], "conflict_files must not be a shared mutable default"


class TestGetResignEmail:
    """Regression tests for #497: resign email is configurable, not hardcoded.

    These exercise email *resolution*; the GPG-key-match guard (#1025) is
    bypassed with FLEET_SKIP_EMAIL_KEY_CHECK so resolution is tested in
    isolation. The guard itself is covered by TestResignEmailKeyGuard.
    """

    def test_env_var_takes_precedence(self, monkeypatch) -> None:
        """FLEET_GIT_EMAIL is used when set."""
        from hephaestus.github.fleet_sync import get_resign_email

        monkeypatch.setenv("FLEET_SKIP_EMAIL_KEY_CHECK", "1")
        monkeypatch.setenv("FLEET_GIT_EMAIL", "alice@example.com")
        assert get_resign_email() == "alice@example.com"

    def test_empty_env_var_falls_through_to_git_config(self, monkeypatch) -> None:
        """An empty FLEET_GIT_EMAIL falls back to git config."""
        from hephaestus.github import fleet_sync

        monkeypatch.setenv("FLEET_SKIP_EMAIL_KEY_CHECK", "1")
        monkeypatch.setenv("FLEET_GIT_EMAIL", "")

        # Stub subprocess.run so the test does not depend on the operator's
        # actual git config.
        class _Result:
            def __init__(self) -> None:
                self.returncode = 0
                self.stdout = "bob@example.com\n"

        # Target the attribute by dotted path so strict mypy (implicit_reexport=False)
        # doesn't complain about fleet_sync not re-exporting `subprocess`.
        monkeypatch.setattr(
            "hephaestus.github.fleet_sync.subprocess.run",
            lambda *a, **k: _Result(),
        )
        assert fleet_sync.get_resign_email() == "bob@example.com"

    def test_no_config_raises_runtime_error(self, monkeypatch) -> None:
        """When nothing is configured, fleet_sync fails loudly rather than guess."""
        import pytest

        from hephaestus.github import fleet_sync

        monkeypatch.delenv("FLEET_GIT_EMAIL", raising=False)

        class _EmptyResult:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(
            "hephaestus.github.fleet_sync.subprocess.run",
            lambda *a, **k: _EmptyResult(),
        )
        with pytest.raises(RuntimeError, match="no resign email configured"):
            fleet_sync.get_resign_email()

    def test_get_resign_exec_embeds_resolved_email(self, monkeypatch) -> None:
        """get_resign_exec() inlines the resolved email into the git command."""
        from hephaestus.github.fleet_sync import get_resign_exec

        monkeypatch.setenv("FLEET_SKIP_EMAIL_KEY_CHECK", "1")
        monkeypatch.setenv("FLEET_GIT_EMAIL", "carol@example.com")
        cmd = get_resign_exec()
        assert "user.email=carol@example.com" in cmd
        assert "commit --amend --no-edit -S -s --reset-author" in cmd


class TestResignEmailKeyGuard:
    """Regression tests for #1025: re-sign email must match the GPG signing key.

    A commit re-signed with an email that is not on the configured signing key
    signs locally but GitHub reports verified=false/reason=no_user, so pr-policy
    rejects the PR at merge. get_resign_email() must catch this and fail fast.
    """

    def _stub_signing_key(self, monkeypatch, *, signingkey: str, uids: list[str]) -> None:
        """Stub git+gpg so the signing key reports ``uids`` as its UID emails."""

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock()
            if cmd[:3] == ["git", "config", "--get"] and cmd[3] == "user.signingkey":
                result.returncode = 0 if signingkey else 1
                result.stdout = f"{signingkey}\n" if signingkey else ""
            elif cmd[:2] == ["gpg", "--list-keys"]:
                result.returncode = 0
                result.stdout = "".join(
                    f"uid:-::::1700000000::HASH::Name <{e}>::::::::::0:\n" for e in uids
                )
            else:
                result.returncode = 1
                result.stdout = ""
            return result

        monkeypatch.setattr("hephaestus.github.fleet_sync.subprocess.run", fake_run)

    def test_email_on_key_is_accepted(self, monkeypatch) -> None:
        """Resolution succeeds when the email is a UID on the signing key."""
        from hephaestus.github import fleet_sync

        monkeypatch.delenv("FLEET_SKIP_EMAIL_KEY_CHECK", raising=False)
        monkeypatch.setenv("FLEET_GIT_EMAIL", "Dev@Example.com")  # case-insensitive
        self._stub_signing_key(monkeypatch, signingkey="ABC123", uids=["dev@example.com"])
        assert fleet_sync.get_resign_email() == "Dev@Example.com"

    def test_email_not_on_key_raises(self, monkeypatch) -> None:
        """A mismatch fails fast with an actionable pr-policy message."""
        from hephaestus.github import fleet_sync

        monkeypatch.delenv("FLEET_SKIP_EMAIL_KEY_CHECK", raising=False)
        monkeypatch.setenv("FLEET_GIT_EMAIL", "bot@users.noreply.github.com")
        self._stub_signing_key(monkeypatch, signingkey="ABC123", uids=["dev@example.com"])
        with pytest.raises(RuntimeError, match="not a UID on the configured"):
            fleet_sync.get_resign_email()

    def test_skip_env_bypasses_check(self, monkeypatch) -> None:
        """FLEET_SKIP_EMAIL_KEY_CHECK lets a mismatched email through."""
        from hephaestus.github import fleet_sync

        monkeypatch.setenv("FLEET_SKIP_EMAIL_KEY_CHECK", "1")
        monkeypatch.setenv("FLEET_GIT_EMAIL", "bot@users.noreply.github.com")
        self._stub_signing_key(monkeypatch, signingkey="ABC123", uids=["dev@example.com"])
        assert fleet_sync.get_resign_email() == "bot@users.noreply.github.com"

    def test_no_signing_key_skips_check(self, monkeypatch) -> None:
        """When no signingkey is configured, the check is skipped (cannot verify)."""
        from hephaestus.github import fleet_sync

        monkeypatch.delenv("FLEET_SKIP_EMAIL_KEY_CHECK", raising=False)
        monkeypatch.setenv("FLEET_GIT_EMAIL", "anything@example.com")
        self._stub_signing_key(monkeypatch, signingkey="", uids=[])
        assert fleet_sync.get_resign_email() == "anything@example.com"

    def test_gpg_missing_skips_check(self, monkeypatch) -> None:
        """When gpg is not installed, the check is skipped rather than blocking."""
        from hephaestus.github import fleet_sync

        monkeypatch.delenv("FLEET_SKIP_EMAIL_KEY_CHECK", raising=False)
        monkeypatch.setenv("FLEET_GIT_EMAIL", "anything@example.com")

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["gpg", "--list-keys"]:
                raise FileNotFoundError("gpg")
            result = MagicMock()
            result.returncode = 0
            result.stdout = "ABC123\n"
            return result

        monkeypatch.setattr("hephaestus.github.fleet_sync.subprocess.run", fake_run)
        assert fleet_sync.get_resign_email() == "anything@example.com"


class TestMain:
    """Smoke tests for hephaestus.github.fleet_sync.main()."""

    def test_main_success_json(self, monkeypatch, capsys) -> None:
        """main() with --json emits ok envelope when no failures occur."""
        from hephaestus.github import fleet_sync

        def fake_process(repo, org, args, clone_dir, *, symbols=None):
            return {
                "merged": 1,
                "rebased": 0,
                "conflict_resolved": 0,
                "skipped": 0,
                "failed": 0,
            }

        monkeypatch.setattr(fleet_sync, "process_repo", fake_process)
        monkeypatch.setattr(
            "sys.argv",
            [
                "fleet-sync",
                "--org",
                "owner",
                "--repos",
                "a",
                "--json",
                "--dry-run",
                "--agent",
                "claude",
            ],
        )
        assert fleet_sync.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["repos"] == 1
        assert payload["totals"]["merged"] == 1

    def test_main_failure_json(self, monkeypatch, capsys) -> None:
        """main() with failures returns 1 and JSON envelope shows error."""
        from hephaestus.github import fleet_sync

        def fake_process(repo, org, args, clone_dir, *, symbols=None):
            return {
                "merged": 0,
                "rebased": 0,
                "conflict_resolved": 0,
                "skipped": 0,
                "failed": 1,
            }

        monkeypatch.setattr(fleet_sync, "process_repo", fake_process)
        monkeypatch.setattr(
            "sys.argv",
            [
                "fleet-sync",
                "--org",
                "owner",
                "--repos",
                "a",
                "--json",
                "--dry-run",
                "--agent",
                "claude",
            ],
        )
        assert fleet_sync.main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["totals"]["failed"] == 1

    def test_main_success_text(self, monkeypatch) -> None:
        """main() without --json still runs through every repo."""
        from hephaestus.github import fleet_sync

        calls = []

        def fake_process(repo, org, args, clone_dir, *, symbols=None):
            calls.append(repo)
            return {
                "merged": 0,
                "rebased": 0,
                "conflict_resolved": 0,
                "skipped": 0,
                "failed": 0,
            }

        monkeypatch.setattr(fleet_sync, "process_repo", fake_process)
        monkeypatch.setattr(
            "sys.argv",
            ["fleet-sync", "--org", "owner", "--repos", "a", "b", "--dry-run", "--agent", "claude"],
        )
        assert fleet_sync.main() == 0
        assert calls == ["a", "b"]


class TestTimeoutHandling:
    """Tests for subprocess timeout handling in fleet_sync."""

    def test_get_resign_email_with_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_resign_email handles TimeoutExpired by trying next config source."""
        monkeypatch.delenv("FLEET_GIT_EMAIL", raising=False)
        # Isolate resolution from the #1025 GPG-key-match guard.
        monkeypatch.setenv("FLEET_SKIP_EMAIL_KEY_CHECK", "1")

        call_count = [0]

        def failing_run(*args, **kwargs):
            call_count[0] += 1
            # First call times out, second returns a result
            if call_count[0] == 1:
                raise subprocess.TimeoutExpired(["git"], 10)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "alice@example.com\n"
            return result

        monkeypatch.setattr("hephaestus.github.fleet_sync.subprocess.run", failing_run)
        # Should get email from second attempt (after timeout)
        assert get_resign_email() == "alice@example.com"

    def test_get_resign_email_uses_metadata_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_resign_email uses METADATA_TIMEOUT."""
        monkeypatch.delenv("FLEET_GIT_EMAIL", raising=False)
        # Isolate resolution from the #1025 GPG-key-match guard.
        monkeypatch.setenv("FLEET_SKIP_EMAIL_KEY_CHECK", "1")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="test@example.com\n")
            get_resign_email()
            assert mock_run.called
            call_kwargs = mock_run.call_args[1]
            assert "timeout" in call_kwargs
            assert call_kwargs["timeout"] == fleet_sync_module.METADATA_TIMEOUT

    def test_gh_delegates_to_gh_call_and_scopes_repo(self) -> None:
        """_gh routes through gh_call with repo scoping and NETWORK_TIMEOUT."""
        completed = subprocess.CompletedProcess(["gh"], 0, stdout="[]", stderr="")
        with patch.object(fleet_sync_module, "gh_call", return_value=completed) as mock_gh_call:
            result = fleet_sync_module._gh(["pr", "list"], repo="TestRepo", org="TestOrg")

        assert result is completed
        mock_gh_call.assert_called_once_with(
            ["--repo", "TestOrg/TestRepo", "pr", "list"],
            check=True,
            timeout=fleet_sync_module.NETWORK_TIMEOUT,
        )

    def test_git_uses_network_timeout(self) -> None:
        """_git function uses NETWORK_TIMEOUT."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            work_dir = Path("/tmp/test")
            fleet_sync_module._git(["clone", "url", "."], cwd=work_dir)
            assert mock_run.called
            call_kwargs = mock_run.call_args[1]
            assert "timeout" in call_kwargs
            assert call_kwargs["timeout"] == fleet_sync_module.NETWORK_TIMEOUT


def _pr(number: int, status: PRStatus, head: str = "feat") -> PRInfo:
    """Build a minimal PRInfo for the given number/status."""
    return PRInfo(
        repo="RepoA",
        number=number,
        title="t",
        head_ref=head,
        base_ref="main",
        head_sha="deadbeef",
        mergeable="MERGEABLE",
        merge_state="CLEAN",
        ci_state="SUCCESS",
        status=status,
    )


@pytest.fixture
def capture_fleet_sync_logs():
    r"""Fixture to capture fleet_sync logger messages.

    Context manager that captures log messages from the fleet_sync module logger
    by attaching a custom handler. Automatically cleans up on exit.

    Yields:
        list: A list that will be populated with log message strings as they are emitted.

    Usage:
        with capture_fleet_sync_logs() as messages:
            # ... code that logs ...
            assert "expected message" in "\n".join(messages)

    """
    import logging
    from contextlib import contextmanager

    @contextmanager
    def _capture():
        messages = []

        class _TestHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        # Get the underlying logger (ContextLogger wraps a LoggerAdapter)
        logger_adapter = fleet_sync_module.logger
        underlying_logger = logger_adapter.logger
        handler = _TestHandler()
        underlying_logger.addHandler(handler)

        try:
            yield messages
        finally:
            underlying_logger.removeHandler(handler)

    return _capture()


class TestCloneReuseAndWorktrees:
    """#1044: clone each repo once, use worktrees per PR instead of re-cloning."""

    def test_ensure_repo_clone_clones_when_absent(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def fake_git(args, cwd, dry_run=False, check=True):
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(fleet_sync_module, "_git", side_effect=fake_git):
            path = fleet_sync_module.ensure_repo_clone("RepoA", "HomericIntelligence", tmp_path)

        assert path == tmp_path / "RepoA"
        assert calls[0][0] == "clone"
        assert not any(a[0] == "fetch" for a in calls)

    def test_ensure_repo_clone_reuses_existing(self, tmp_path: Path) -> None:
        # Simulate an already-present clone.
        (tmp_path / "RepoA" / ".git").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_git(args, cwd, dry_run=False, check=True):
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(fleet_sync_module, "_git", side_effect=fake_git):
            path = fleet_sync_module.ensure_repo_clone("RepoA", "HomericIntelligence", tmp_path)

        assert path == tmp_path / "RepoA"
        # Reuse path fetches, never clones.
        assert not any(a[0] == "clone" for a in calls)
        assert calls[0][:2] == ["fetch", "--prune"]

    def test_add_pr_worktree_adds_off_clone(self, tmp_path: Path) -> None:
        repo_clone = tmp_path / "RepoA"
        work = tmp_path / "RepoA-7"
        calls: list[list[str]] = []

        def fake_git(args, cwd, dry_run=False, check=True):
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(fleet_sync_module, "_git", side_effect=fake_git):
            fleet_sync_module.add_pr_worktree(repo_clone, work, "feat", "main")

        assert ["fetch", "origin", "feat"] in calls
        assert ["fetch", "origin", "main"] in calls
        worktree_add = [a for a in calls if a[:2] == ["worktree", "add"]]
        assert len(worktree_add) == 1
        assert str(work) in worktree_add[0]
        assert "origin/feat" in worktree_add[0]

    def test_rebase_and_resign_uses_worktree_not_clone(self, tmp_path: Path) -> None:
        repo_clone = tmp_path / "RepoA"
        pr = _pr(7, PRStatus.OUTDATED)
        # Pre-create the per-PR work dir so the cleanup removal actually fires
        # (remove_worktree is a no-op when the path is absent).
        (tmp_path / "RepoA-7").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_git(args, cwd, dry_run=False, check=True):
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch.object(fleet_sync_module, "_git", side_effect=fake_git),
            patch.object(fleet_sync_module, "get_resign_exec", return_value="true"),
        ):
            ok = fleet_sync_module.rebase_and_resign(pr, repo_clone)

        assert ok is True
        # Never clones; uses a worktree and cleans it up.
        assert not any(a[0] == "clone" for a in calls)
        assert any(a[:2] == ["worktree", "add"] for a in calls)
        assert any(a[:2] == ["worktree", "remove"] for a in calls)

    def test_process_repo_clones_once_for_multiple_prs(self, tmp_path: Path) -> None:
        """Two OUTDATED PRs in one repo must trigger exactly one clone."""
        prs = [_pr(n, PRStatus.OUTDATED, head=f"feat{n}") for n in (1, 2, 3)]
        clone_count = [0]

        def fake_ensure(repo, org, clone_dir, dry_run=False):
            clone_count[0] += 1
            return clone_dir / repo

        args = MagicMock(dry_run=False, skip_conflict_resolution=False, agent="claude")

        with (
            patch.object(fleet_sync_module, "list_prs", return_value=prs),
            patch.object(fleet_sync_module, "ensure_repo_clone", side_effect=fake_ensure),
            patch.object(fleet_sync_module, "rebase_and_resign", return_value=True),
        ):
            counts = fleet_sync_module.process_repo("RepoA", "HomericIntelligence", args, tmp_path)

        assert clone_count[0] == 1
        assert counts["rebased"] == 3

    def test_process_repo_skips_clone_when_no_checkout_needed(self, tmp_path: Path) -> None:
        """READY-only PRs merge via gh and never trigger a clone."""
        prs = [_pr(1, PRStatus.READY, head="feat1")]
        clone_count = [0]

        def fake_ensure(repo, org, clone_dir, dry_run=False):
            clone_count[0] += 1
            return clone_dir / repo

        args = MagicMock(dry_run=False, skip_conflict_resolution=False, agent="claude")

        with (
            patch.object(fleet_sync_module, "list_prs", return_value=prs),
            patch.object(fleet_sync_module, "ensure_repo_clone", side_effect=fake_ensure),
            patch.object(fleet_sync_module, "merge_pr", return_value=True),
        ):
            counts = fleet_sync_module.process_repo("RepoA", "HomericIntelligence", args, tmp_path)

        assert clone_count[0] == 0
        assert counts["merged"] == 1


class TestMergePr:
    """Merge-path error handling."""

    def test_merge_pr_handles_runtime_errors_from_gh_call(self) -> None:
        """Circuit-breaker/runtime failures return False like gh CLI failures."""
        pr = _pr(42, PRStatus.READY)

        with patch.object(fleet_sync_module, "_gh", side_effect=RuntimeError("breaker open")):
            assert fleet_sync_module.merge_pr(pr, "HomericIntelligence") is False


class TestListPrs:
    """Regression tests for #1027: statusCheckRollup must not be bulk-fetched.

    Requesting statusCheckRollup for every open PR in one `gh pr list` call 504s
    at scale. The bulk list omits it; CI state is fetched per-PR. A genuine list
    failure must raise, never be swallowed into an empty list.
    """

    def test_bulk_list_omits_statuscheckrollup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bulk `gh pr list --json` must NOT request statusCheckRollup."""
        captured: dict[str, list[str]] = {}

        def fake_gh(args, repo=None, **kwargs):
            if args[:2] == ["pr", "list"]:
                captured["list_args"] = args
                return MagicMock(
                    stdout=json.dumps(
                        [
                            {
                                "number": 1,
                                "title": "t",
                                "headRefName": "h",
                                "baseRefName": "main",
                                "headRefOid": "sha",
                                "mergeable": "MERGEABLE",
                                "mergeStateStatus": "BEHIND",
                            }
                        ]
                    )
                )
            # pr view (per-PR CI fetch)
            return MagicMock(stdout=json.dumps({"statusCheckRollup": []}))

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)
        prs = fleet_sync_module.list_prs("ProjectHephaestus", "HomericIntelligence")
        json_idx = captured["list_args"].index("--json")
        json_fields = captured["list_args"][json_idx + 1]
        assert "statusCheckRollup" not in json_fields
        assert len(prs) == 1
        assert prs[0].number == 1

    def test_ci_state_fetched_per_pr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CI state (statusCheckRollup) is fetched via a per-PR `gh pr view`."""
        view_calls: list[list[str]] = []

        def fake_gh(args, repo=None, **kwargs):
            if args[:2] == ["pr", "list"]:
                return MagicMock(
                    stdout=json.dumps(
                        [
                            {
                                "number": 7,
                                "title": "t",
                                "headRefName": "h",
                                "baseRefName": "main",
                                "headRefOid": "sha",
                                "mergeable": "MERGEABLE",
                                "mergeStateStatus": "CLEAN",
                            }
                        ]
                    )
                )
            view_calls.append(args)
            return MagicMock(
                stdout=json.dumps(
                    {"statusCheckRollup": [{"conclusion": "SUCCESS", "state": "SUCCESS"}]}
                )
            )

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)
        prs = fleet_sync_module.list_prs("ProjectHephaestus", "HomericIntelligence")
        assert view_calls and view_calls[0][:3] == ["pr", "view", "7"]
        assert prs[0].ci_state == "SUCCESS"
        assert prs[0].status == PRStatus.READY

    def test_per_pr_ci_failure_returns_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flaky per-PR CI fetch downgrades to UNKNOWN, not a whole-run abort."""

        def fake_gh(args, repo=None, **kwargs):
            if args[:2] == ["pr", "list"]:
                return MagicMock(
                    stdout=json.dumps(
                        [
                            {
                                "number": 9,
                                "title": "t",
                                "headRefName": "h",
                                "baseRefName": "main",
                                "headRefOid": "sha",
                                "mergeable": "MERGEABLE",
                                "mergeStateStatus": "BEHIND",
                            }
                        ]
                    )
                )
            raise RuntimeError("504 on pr view")

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)
        prs = fleet_sync_module.list_prs("ProjectHephaestus", "HomericIntelligence")
        assert prs[0].ci_state == "UNKNOWN"

    def test_list_failure_raises_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A genuine bulk-list failure raises rather than returning []."""

        def fake_gh(args, repo=None, **kwargs):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 504")

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)
        with pytest.raises(RuntimeError, match="could not list PRs"):
            fleet_sync_module.list_prs("ProjectHephaestus", "HomericIntelligence")

    def test_empty_list_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An actually-empty repo returns [] (distinct from a list failure)."""

        def fake_gh(args, repo=None, **kwargs):
            return MagicMock(stdout="[]")

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)
        assert fleet_sync_module.list_prs("ProjectHephaestus", "HomericIntelligence") == []


class TestPrClassification:
    """Regression tests for #1029: stale-failing PRs must be rebased, not skipped.

    A FAILING classification (skip) must require the branch to be up to date with
    its base (mergeStateStatus CLEAN). A PR that is BEHIND or BLOCKED with a red
    CI result has stale checks (ran against an old base, often a failure already
    fixed on main) and must classify as OUTDATED so it gets rebased and re-run —
    otherwise a fix landing on main strands the entire queue as FAILING.
    """

    def _classify(self, monkeypatch, *, mergeable: str, state: str, ci: str):
        """Run list_prs with a single stubbed PR and return its PRStatus."""

        def fake_gh(args, repo=None, **kwargs):
            if args[:2] == ["pr", "list"]:
                return MagicMock(
                    stdout=json.dumps(
                        [
                            {
                                "number": 1,
                                "title": "t",
                                "headRefName": "h",
                                "baseRefName": "main",
                                "headRefOid": "sha",
                                "mergeable": mergeable,
                                "mergeStateStatus": state,
                            }
                        ]
                    )
                )
            # per-PR CI fetch: map desired ci_state to a rollup
            rollup = []
            if ci == "FAILURE":
                rollup = [{"conclusion": "FAILURE", "state": "FAILURE"}]
            elif ci == "SUCCESS":
                rollup = [{"conclusion": "SUCCESS", "state": "SUCCESS"}]
            return MagicMock(stdout=json.dumps({"statusCheckRollup": rollup}))

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)
        return fleet_sync_module.list_prs("ProjectHephaestus", "HomericIntelligence")[0].status

    def test_blocked_mergeable_failing_is_outdated(self, monkeypatch) -> None:
        """BLOCKED+MERGEABLE with red CI = stale failure → rebase (OUTDATED)."""
        assert (
            self._classify(monkeypatch, mergeable="MERGEABLE", state="BLOCKED", ci="FAILURE")
            == PRStatus.OUTDATED
        )

    def test_behind_failing_is_outdated(self, monkeypatch) -> None:
        """BEHIND with red CI = stale failure → rebase (OUTDATED)."""
        assert (
            self._classify(monkeypatch, mergeable="MERGEABLE", state="BEHIND", ci="FAILURE")
            == PRStatus.OUTDATED
        )

    def test_clean_failing_is_failing(self, monkeypatch) -> None:
        """CLEAN (up to date) with red CI = genuine PR failure → skip (FAILING)."""
        assert (
            self._classify(monkeypatch, mergeable="MERGEABLE", state="CLEAN", ci="FAILURE")
            == PRStatus.FAILING
        )

    def test_conflicting_is_conflicted(self, monkeypatch) -> None:
        """CONFLICTING always classifies as CONFLICTED regardless of CI."""
        assert (
            self._classify(monkeypatch, mergeable="CONFLICTING", state="DIRTY", ci="FAILURE")
            == PRStatus.CONFLICTED
        )

    def test_clean_success_is_ready(self, monkeypatch) -> None:
        """CLEAN + green CI = READY."""
        assert (
            self._classify(monkeypatch, mergeable="MERGEABLE", state="CLEAN", ci="SUCCESS")
            == PRStatus.READY
        )


class TestListPrsAuthorScope:
    """list_prs must only ever surface PRs authored by the current user.

    Regression guard for #1070: fleet_sync rebases and re-signs every PR it
    lists, which on a Dependabot (or any other author's) PR strips the native
    signature and stamps the local identity — silently producing an UNSIGNED
    commit that blocks merge. Scoping discovery to ``--author @me`` means the
    automation never touches a PR the current user did not author.
    """

    def test_list_prs_filters_to_current_user(self, monkeypatch) -> None:
        """The ``gh pr list`` argv must include ``--author @me``."""
        captured_argv: list[list[str]] = []

        def fake_gh(args, repo=None, org=None, **kwargs):
            captured_argv.append(args)
            return MagicMock(stdout="[]")

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)

        fleet_sync_module.list_prs("RepoA", "HomericIntelligence")

        assert captured_argv, "_gh was never called"
        pr_list_argv = captured_argv[0]
        assert "--author" in pr_list_argv, pr_list_argv
        author_value = pr_list_argv[pr_list_argv.index("--author") + 1]
        assert author_value == "@me", pr_list_argv

    def test_non_self_authored_pr_is_never_returned(self, monkeypatch) -> None:
        """A PR returned by gh is still surfaced (gh applies the @me filter).

        gh resolves ``--author @me`` server-side, so by the time the JSON comes
        back it already excludes other authors. This asserts the wiring: the
        author filter rides on the same call that returns the list, so no
        separate client-side filtering can drift out of sync.
        """
        monkeypatch.setattr(
            fleet_sync_module,
            "_fetch_pr_ci_state",
            lambda repo, number, org=None: "SUCCESS",
        )

        def fake_gh(args, repo=None, org=None, **kwargs):
            assert "--author" in args and args[args.index("--author") + 1] == "@me"
            payload = [
                {
                    "number": 1,
                    "title": "mine",
                    "headRefName": "feat",
                    "baseRefName": "main",
                    "headRefOid": "deadbeef",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            ]
            return MagicMock(stdout=json.dumps(payload))

        monkeypatch.setattr(fleet_sync_module, "_gh", fake_gh)

        prs = fleet_sync_module.list_prs("RepoA", "HomericIntelligence")

        assert [p.number for p in prs] == [1]


class TestAsciiFlag:
    """--ascii swaps Unicode glyphs in log output for portable ASCII."""

    def test_symbols_dataclass_is_frozen(self) -> None:
        """Symbols instances are frozen to prevent accidental mutation."""
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            fleet_sync_module.ASCII_SYMBOLS.check = "X"

    def test_presets_have_expected_glyphs(self) -> None:
        """Unicode and ASCII symbol presets have the correct glyphs."""
        assert fleet_sync_module.UNICODE_SYMBOLS.check == "✓"
        assert fleet_sync_module.UNICODE_SYMBOLS.banner == "══"
        assert fleet_sync_module.UNICODE_SYMBOLS.arrow == "→"
        assert fleet_sync_module.UNICODE_SYMBOLS.dash == "—"
        assert fleet_sync_module.ASCII_SYMBOLS.check == "*"
        assert fleet_sync_module.ASCII_SYMBOLS.banner == "=="
        assert fleet_sync_module.ASCII_SYMBOLS.arrow == "->"
        assert fleet_sync_module.ASCII_SYMBOLS.dash == "--"

    def test_process_repo_emits_ascii_banner_when_ascii_symbols_passed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capture_fleet_sync_logs
    ) -> None:
        """process_repo logs ASCII banner under ASCII_SYMBOLS — no module state."""
        monkeypatch.setattr(fleet_sync_module, "list_prs", lambda _repo, _org: [])

        import argparse

        with capture_fleet_sync_logs as logged_messages:
            args = argparse.Namespace(
                dry_run=True,
                skip_conflict_resolution=True,
                agent="claude",
                json=False,
                verbose=False,
                ascii=True,
            )

            fleet_sync_module.process_repo(
                "test-repo",
                "HomericIntelligence",
                args,
                tmp_path,
                symbols=fleet_sync_module.ASCII_SYMBOLS,
            )

            # Check logged messages
            output = "\n".join(logged_messages)
            assert "== test-repo ==" in output, f"Expected ASCII banner in: {output}"
            assert "══" not in output, f"Unexpected Unicode banner in: {output}"

    def test_process_repo_emits_unicode_banner_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capture_fleet_sync_logs
    ) -> None:
        """Default kwarg gives Unicode — backward-compat for existing callers."""
        monkeypatch.setattr(fleet_sync_module, "list_prs", lambda _repo, _org: [])

        import argparse

        with capture_fleet_sync_logs as logged_messages:
            args = argparse.Namespace(
                dry_run=True,
                skip_conflict_resolution=True,
                agent="claude",
                json=False,
                verbose=False,
                ascii=False,
            )

            fleet_sync_module.process_repo("test-repo", "HomericIntelligence", args, tmp_path)

            # Check logged messages
            output = "\n".join(logged_messages)
            assert "══ test-repo ══" in output, f"Expected Unicode banner in: {output}"

    def test_ascii_flag_registered_on_production_parser(self) -> None:
        """The real parser registers --ascii as a default-False store_true flag.

        Guards against the flag being dropped from ``main`` without a test
        failing — the behavioral tests pass ``symbols=`` directly and bypass
        argparse entirely.
        """
        parser = fleet_sync_module._build_parser()

        ascii_action = next((a for a in parser._actions if "--ascii" in a.option_strings), None)
        assert ascii_action is not None, "--ascii flag is not registered on the parser"
        assert ascii_action.default is False
        assert ascii_action.const is True  # store_true

        # Parsing without the flag yields ascii=False; with it, ascii=True.
        assert parser.parse_args([]).ascii is False
        assert parser.parse_args(["--ascii"]).ascii is True

    def test_ascii_help_describes_glyph_swap_not_identity_mapping(self) -> None:
        """--ascii help text describes what it replaces, not ASCII->ASCII noise.

        Regression guard for the self-contradictory ``== for ==`` mapping that
        conveyed nothing about what the flag swaps.
        """
        parser = fleet_sync_module._build_parser()
        ascii_action = next(a for a in parser._actions if "--ascii" in a.option_strings)
        help_text = ascii_action.help or ""

        assert "Unicode" in help_text, "help must name what is being replaced"
        # The garbled self-mapping must not reappear.
        assert "== for ==" not in help_text
        assert "* for *" not in help_text
