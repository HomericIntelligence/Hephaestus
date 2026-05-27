"""Unit tests for hephaestus.github.fleet_sync — pure logic functions."""

from __future__ import annotations

from hephaestus.github.fleet_sync import PRInfo, PRStatus, _ci_state


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
    """Regression tests for #497: resign email is configurable, not hardcoded."""

    def test_env_var_takes_precedence(self, monkeypatch) -> None:
        """FLEET_GIT_EMAIL is used when set."""
        from hephaestus.github.fleet_sync import get_resign_email

        monkeypatch.setenv("FLEET_GIT_EMAIL", "alice@example.com")
        assert get_resign_email() == "alice@example.com"

    def test_empty_env_var_falls_through_to_git_config(self, monkeypatch) -> None:
        """An empty FLEET_GIT_EMAIL falls back to git config."""
        from hephaestus.github import fleet_sync

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

        monkeypatch.setenv("FLEET_GIT_EMAIL", "carol@example.com")
        cmd = get_resign_exec()
        assert "user.email=carol@example.com" in cmd
        assert "commit --amend --no-edit -S --reset-author" in cmd


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
        monkeypatch.setattr("sys.argv", ["fleet-sync", "--repos", "owner/a", "--json", "--dry-run"])
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
        monkeypatch.setattr("sys.argv", ["fleet-sync", "--repos", "owner/a", "--json", "--dry-run"])
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
            ["fleet-sync", "--repos", "owner/a", "owner/b", "--dry-run"],
        )
        assert fleet_sync.main() == 0
        assert calls == ["owner/a", "owner/b"]
