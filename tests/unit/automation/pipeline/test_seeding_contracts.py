"""Contract tests for pipeline seeding public helpers."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from hephaestus.automation.pipeline.seeding import seed_issue_from_github
from hephaestus.automation.state_labels import STATE_PLAN_GO


class TestSeedIssueFromGitHubContract:
    """Repo-scoped seeding mirrors the seed_issue tri-state/fail-closed contract."""

    def _github(self) -> MagicMock:
        github = MagicMock()
        github.gh_issue_json.return_value = {
            "number": 104,
            "title": "A task",
            "body": "",
            "labels": [{"name": STATE_PLAN_GO}],
        }
        github.find_pr_for_issue.return_value = None
        github.find_merged_pr_for_issue.return_value = None
        return github

    def test_docstring_documents_tri_state_fail_closed_and_raises(self) -> None:
        """The repo-scoped helper documents the duplicate-PR prevention contract."""
        doc = inspect.getdoc(seed_issue_from_github) or ""

        for expected in (
            "tri-state",
            "find_pr_for_issue",
            "find_merged_pr_for_issue",
            "Fail-closed",
            "IMPLEMENTATION",
            "Raises:",
        ):
            assert expected in doc

    def test_issue_fetch_failure_raises(self) -> None:
        """gh_issue_json failures propagate instead of producing fallback facts."""
        github = self._github()
        github.gh_issue_json.side_effect = RuntimeError("issue fetch down")

        with pytest.raises(RuntimeError, match="issue fetch down"):
            seed_issue_from_github(104, github)

    def test_open_pr_lookup_failure_raises(self) -> None:
        """Open-PR probe failures propagate, never falling back to no-PR facts."""
        github = self._github()
        github.find_pr_for_issue.side_effect = RuntimeError("open probe down")

        with pytest.raises(RuntimeError, match="open probe down"):
            seed_issue_from_github(104, github)

    def test_merged_pr_lookup_failure_raises(self) -> None:
        """Merged-PR probe failures propagate after an open-PR miss."""
        github = self._github()
        github.find_merged_pr_for_issue.side_effect = RuntimeError("merged probe down")

        with pytest.raises(RuntimeError, match="merged probe down"):
            seed_issue_from_github(104, github)
