"""Tests for GraphQL parameterisation in PR review-thread helpers (#738)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.github_api import (
    _review_threads_for_review,
    gh_pr_list_unresolved_threads,
)


class TestReviewThreadsForReviewParameterisation:
    """Tests for _review_threads_for_review parameterisation."""

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_fetches_matching_review_thread_after_first_hundred(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """Receipt lookup paginates without accepting another review's threads."""
        mock_repo_info.return_value = ("owner", "repo")
        first_page_nodes = [
            {
                "id": f"T_other_{index}",
                "isResolved": False,
                "comments": {"nodes": [{"pullRequestReview": {"id": "OTHER_REVIEW"}}]},
            }
            for index in range(100)
        ]
        second_page_nodes = [
            {
                "id": "T_mine",
                "isResolved": False,
                "comments": {"nodes": [{"pullRequestReview": {"id": "REVIEW_1"}}]},
            },
            {
                "id": "T_foreign",
                "isResolved": False,
                "comments": {"nodes": [{"pullRequestReview": {"id": "OTHER_REVIEW"}}]},
            },
        ]

        def side_effect(argv: list[str], **_: Any) -> Mock:
            after_first_page = "after=cursor-1" in argv
            result = Mock()
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": (
                                        second_page_nodes if after_first_page else first_page_nodes
                                    ),
                                    "pageInfo": {
                                        "hasNextPage": not after_first_page,
                                        "endCursor": (None if after_first_page else "cursor-1"),
                                    },
                                }
                            }
                        }
                    }
                }
            )
            return result

        mock_gh_call.side_effect = side_effect

        assert _review_threads_for_review(42, "REVIEW_1") == ["T_mine"]
        assert mock_gh_call.call_count == 2
        assert "after=cursor-1" in mock_gh_call.call_args_list[1].args[0]

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
        # The query must request thread side and all comment authors so cleanup
        # can reason over the full thread, not just the first flattened author.
        assert "author{ login }" in query
        assert "side:diffSide" in query
        assert "comments(first:20)" in query
        assert "pageInfo{ hasNextPage" in query

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_fails_closed_when_comment_ownership_is_truncated(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """A human reply after 20 bot comments cannot be omitted from ownership."""
        mock_repo_info.return_value = ("owner", "repo")
        all_comments = [
            {"body": f"bot reply {index}", "author": {"login": "ci-bot"}} for index in range(20)
        ]
        all_comments.append({"body": "human reply", "author": {"login": "reviewer"}})
        mock_result = Mock()
        mock_result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "T1",
                                        "isResolved": False,
                                        "comments": {
                                            "pageInfo": {
                                                "hasNextPage": len(all_comments) > 20,
                                                "endCursor": "comment-cursor-20",
                                            },
                                            "nodes": all_comments[:20],
                                        },
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        )
        mock_gh_call.return_value = mock_result

        with pytest.raises(RuntimeError, match=r"could not fetch all comments.*T1"):
            gh_pr_list_unresolved_threads(42)

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
                "side": "RIGHT",
                "comments": {
                    "nodes": [
                        {"body": "nit", "author": {"login": "coderabbitai[bot]"}},
                        {"body": "reply", "author": {"login": "mvillmow"}},
                    ]
                },
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
        assert by_id["T_bot"]["authors"] == ["coderabbitai[bot]", "mvillmow"]
        assert by_id["T_bot"]["comments"] == [
            {"body": "nit", "author": "coderabbitai[bot]"},
            {"body": "reply", "author": "mvillmow"},
        ]
        assert by_id["T_bot"]["side"] == "RIGHT"
        assert by_id["T_human"]["author"] == "alice"
        assert by_id["T_noauthor"]["author"] == ""
