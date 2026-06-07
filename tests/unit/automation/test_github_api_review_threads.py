"""Tests for GraphQL parameterisation in PR review-thread helpers (#738)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock, patch

from hephaestus.automation.github_api import (
    _review_threads_for_review,
    gh_pr_list_unresolved_threads,
)


class TestReviewThreadsForReviewParameterisation:
    """Tests for _review_threads_for_review parameterisation."""

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_uses_parameterised_query(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
        )
        mock_gh_call.return_value = mock_result

        _review_threads_for_review(42, "RV_kw1")

        argv = mock_gh_call.call_args[0][0]
        query = next(a for a in argv if a.startswith("query="))
        assert "$number:Int!" in query
        assert "pullRequest(number:$number)" in query
        assert "pullRequest(number: 42)" not in query  # regression guard
        assert 'owner: "owner"' not in query
        assert "owner=owner" in argv and "name=repo" in argv and "number=42" in argv


class TestListUnresolvedThreadsParameterisation:
    """Tests for gh_pr_list_unresolved_threads parameterisation."""

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_uses_parameterised_query(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
        )
        mock_gh_call.return_value = mock_result

        gh_pr_list_unresolved_threads(42)

        argv = mock_gh_call.call_args[0][0]
        query = next(a for a in argv if a.startswith("query="))
        assert "$number:Int!" in query
        assert "pullRequest(number:$number)" in query
        assert "pullRequest(number: 42)" not in query  # regression guard
        assert 'owner: "owner"' not in query
        assert "owner=owner" in argv and "name=repo" in argv and "number=42" in argv
        # The query must request the first comment's author login (#PR3).
        assert "author{ login }" in query

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_surfaces_comment_author_login(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """Each unresolved thread dict carries the first comment's author login."""
        mock_repo_info.return_value = ("owner", "repo")
        nodes = [
            {
                "id": "T_bot",
                "isResolved": False,
                "path": "a.py",
                "line": 3,
                "comments": {"nodes": [{"body": "nit", "author": {"login": "coderabbitai[bot]"}}]},
            },
            {
                "id": "T_human",
                "isResolved": False,
                "path": "b.py",
                "line": None,
                "comments": {"nodes": [{"body": "hmm", "author": {"login": "alice"}}]},
            },
            {
                "id": "T_noauthor",
                "isResolved": False,
                "path": "c.py",
                "line": 1,
                "comments": {"nodes": [{"body": "x", "author": None}]},
            },
        ]
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
        )
        mock_gh_call.return_value = mock_result

        threads = gh_pr_list_unresolved_threads(42)

        by_id = {t["id"]: t for t in threads}
        assert by_id["T_bot"]["author"] == "coderabbitai[bot]"
        assert by_id["T_human"]["author"] == "alice"
        assert by_id["T_noauthor"]["author"] == ""
