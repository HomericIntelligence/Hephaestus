"""Unit tests for hephaestus.github.fleet_sync — pure logic functions and timeouts."""

from __future__ import annotations

import importlib
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
        assert "commit --amend --no-edit -S --reset-author" in cmd


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
        import json

        from hephaestus.github import fleet_sync

        def fake_process(repo, args, clone_dir):
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
            ["fleet-sync", "--repos", "owner/a", "--json", "--dry-run", "--agent", "claude"],
        )
        assert fleet_sync.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["repos"] == 1
        assert payload["totals"]["merged"] == 1

    def test_main_failure_json(self, monkeypatch, capsys) -> None:
        """main() with failures returns 1 and JSON envelope shows error."""
        import json

        from hephaestus.github import fleet_sync

        def fake_process(repo, args, clone_dir):
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
            ["fleet-sync", "--repos", "owner/a", "--json", "--dry-run", "--agent", "claude"],
        )
        assert fleet_sync.main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["totals"]["failed"] == 1

    def test_main_success_text(self, monkeypatch) -> None:
        """main() without --json still runs through every repo."""
        from hephaestus.github import fleet_sync

        calls = []

        def fake_process(repo, args, clone_dir):
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
            ["fleet-sync", "--repos", "owner/a", "owner/b", "--dry-run", "--agent", "claude"],
        )
        assert fleet_sync.main() == 0
        assert calls == ["owner/a", "owner/b"]


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

    def test_gh_uses_network_timeout(self) -> None:
        """_gh function uses NETWORK_TIMEOUT."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            fleet_sync_module._gh(["pr", "list"], repo="TestRepo")
            assert mock_run.called
            call_kwargs = mock_run.call_args[1]
            assert "timeout" in call_kwargs
            assert call_kwargs["timeout"] == fleet_sync_module.NETWORK_TIMEOUT

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
