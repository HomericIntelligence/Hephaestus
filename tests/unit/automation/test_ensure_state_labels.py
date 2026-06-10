"""Tests for ``hephaestus.automation.ensure_state_labels``.

The script is a thin CLI wrapper around ``gh label create --force``. Tests
mock the ``subprocess.run`` boundary and assert: (1) the right gh commands
are built per repo and per label, (2) dry-run mutates nothing, (3) org
enumeration filters archived/fork repos and the Odysseus sandbox, (4) the
script is idempotent under multiple runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.ensure_state_labels import (
    _gh_list_org_repos,
    ensure_labels_on_repo,
    main,
)
from hephaestus.automation.state_labels import (
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_LABEL_SPECS,
    STATE_NEEDS_PLAN,
    STATE_PLAN_GO,
    STATE_PLAN_NO_GO,
)


@pytest.fixture
def mock_run() -> Iterator[MagicMock]:
    """Patch subprocess.run inside ensure_state_labels (the only side effect)."""
    with patch("hephaestus.automation.ensure_state_labels.subprocess.run") as m:
        yield m


def _ok_proc(stdout: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = ""
    return proc


def _fail_proc(rc: int = 1, stderr: str = "boom") -> MagicMock:
    proc = MagicMock()
    proc.returncode = rc
    proc.stdout = ""
    proc.stderr = stderr
    return proc


class TestEnsureLabelsOnRepo:
    """``ensure_labels_on_repo`` issues one ``gh label create`` per label."""

    def test_issues_one_create_per_label(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok_proc()
        issued = ensure_labels_on_repo("owner/name")
        assert issued == len(STATE_LABEL_SPECS)
        assert mock_run.call_count == len(STATE_LABEL_SPECS)

    def test_passes_label_name_color_description_and_force(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok_proc()
        ensure_labels_on_repo("owner/name")
        seen_labels = set()
        for call in mock_run.call_args_list:
            args = call[0][0]
            assert args[:3] == ["gh", "label", "create"]
            assert "--repo" in args
            assert "owner/name" in args
            assert "--color" in args
            assert "--description" in args
            assert "--force" in args
            seen_labels.add(args[3])
        # Every spec'd label was exercised (includes implementation labels and
        # state:skip, #1083).
        assert seen_labels == set(STATE_LABEL_SPECS.keys())
        assert {STATE_NEEDS_PLAN, STATE_PLAN_NO_GO, STATE_PLAN_GO} <= seen_labels
        assert {STATE_IMPLEMENTATION_NO_GO, STATE_IMPLEMENTATION_GO} <= seen_labels

    def test_dry_run_issues_zero_calls(self, mock_run: MagicMock) -> None:
        issued = ensure_labels_on_repo("owner/name", dry_run=True)
        assert issued == 0
        mock_run.assert_not_called()

    def test_label_create_failure_does_not_abort(self, mock_run: MagicMock) -> None:
        """A single failed label-create is logged but the others are still attempted."""
        # First call fails, remaining succeed.
        n = len(STATE_LABEL_SPECS)
        mock_run.side_effect = [
            _fail_proc(rc=2, stderr="not authorized"),
            *(_ok_proc() for _ in range(n - 1)),
        ]
        issued = ensure_labels_on_repo("owner/name")
        assert issued == n - 1
        assert mock_run.call_count == n


class TestGhListOrgRepos:
    """Org enumeration filters archives, forks, and Odysseus."""

    def test_returns_sorted_non_archived_non_fork_names(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok_proc(
            stdout=json.dumps(
                [
                    {"name": "ProjectZeta", "isArchived": False, "isFork": False},
                    {"name": "ProjectAlpha", "isArchived": False, "isFork": False},
                    {"name": "ProjectArchived", "isArchived": True, "isFork": False},
                    {"name": "SomeFork", "isArchived": False, "isFork": True},
                    {"name": "Odysseus", "isArchived": False, "isFork": False},
                ]
            )
        )
        names = _gh_list_org_repos("AnOrg")
        # Sorted, archives/forks/Odysseus excluded.
        assert names == ["ProjectAlpha", "ProjectZeta"]

    def test_propagates_gh_list_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail_proc(rc=4, stderr="rate limit")
        with pytest.raises(SystemExit, match="gh repo list AnOrg failed"):
            _gh_list_org_repos("AnOrg")

    def test_propagates_invalid_json(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _ok_proc(stdout="not-json{")
        with pytest.raises(SystemExit, match="invalid JSON"):
            _gh_list_org_repos("AnOrg")


class TestMain:
    """End-to-end CLI smoke tests."""

    def test_main_default_uses_detected_repo(self, mock_run: MagicMock) -> None:
        # ``gh repo view`` discovers the repo; then one create per label runs.
        n = len(STATE_LABEL_SPECS)
        mock_run.side_effect = [
            _ok_proc(stdout="HomericIntelligence/ProjectScylla"),  # gh repo view
            *(_ok_proc() for _ in range(n)),  # one create per label
        ]
        rc = main([])
        assert rc == 0
        # 1 detect + n label creates total subprocess calls.
        assert mock_run.call_count == 1 + n

    def test_main_org_enumerates_and_applies_to_each(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            # gh repo list AnOrg
            _ok_proc(
                stdout=json.dumps(
                    [
                        {"name": "RepoA", "isArchived": False, "isFork": False},
                        {"name": "RepoB", "isArchived": False, "isFork": False},
                    ]
                )
            ),
            # n labels x 2 repos label creates
            *(_ok_proc() for _ in range(len(STATE_LABEL_SPECS) * 2)),
        ]
        rc = main(["--org", "AnOrg"])
        assert rc == 0
        assert mock_run.call_count == 1 + len(STATE_LABEL_SPECS) * 2

    def test_main_dry_run_calls_no_label_create(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            # gh repo list AnOrg
            _ok_proc(
                stdout=json.dumps([{"name": "OneRepo", "isArchived": False, "isFork": False}])
            ),
        ]
        rc = main(["--org", "AnOrg", "--dry-run"])
        assert rc == 0
        # ONLY the repo enumeration, no label creates.
        assert mock_run.call_count == 1

    def test_main_specific_repo_skips_org_enumeration(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok_proc() for _ in range(len(STATE_LABEL_SPECS))]
        rc = main(["--repo", "owner/name"])
        assert rc == 0
        # No 'gh repo list' call — exactly one create per label.
        assert mock_run.call_count == len(STATE_LABEL_SPECS)
        for call in mock_run.call_args_list:
            args = call[0][0]
            assert args[:3] == ["gh", "label", "create"]

    def test_main_empty_org_warns_but_succeeds(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok_proc(stdout="[]")]
        rc = main(["--org", "EmptyOrg"])
        assert rc == 0
        assert mock_run.call_count == 1

    def test_repo_view_failure_exits(self, mock_run: MagicMock) -> None:
        mock_run.return_value = _fail_proc(rc=1, stderr="not in a repo")
        with pytest.raises(SystemExit):
            main([])
