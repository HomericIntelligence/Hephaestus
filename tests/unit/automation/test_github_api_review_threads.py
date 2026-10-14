"""Tests for GraphQL parameterisation in PR review-thread helpers (#738)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.github_api import (
    GitHubRateLimitError,
    GitHubUnavailableError,
    GraphQLDeterministicError,
    GraphQLRetryableError,
    _review_threads_for_review,
    gh_pr_list_unresolved_threads,
)
from hephaestus.automation.github_api.threads import _complete_thread_snapshot


def _inline_thread_node(
    thread_id: str,
    *,
    review_id: str = "REVIEW_1",
    body: str = "finding",
) -> dict[str, Any]:
    """Build a complete root-comment node for inline review helper tests."""
    return {
        "id": thread_id,
        "isResolved": False,
        "path": "a.py",
        "line": 1,
        "side": "RIGHT",
        "comments": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "id": f"C-{thread_id}",
                    "body": body,
                    "viewerCanUpdate": True,
                    "pullRequestReview": {"id": review_id},
                }
            ],
        },
    }


def _thread_page_payload(
    nodes: list[dict[str, Any]],
    *,
    pr_number: int = 42,
    owner: str = "owner",
    name: str = "repo",
    has_next: bool = False,
    end_cursor: str | None = None,
) -> str:
    """Build a complete repository/PR/thread page for strict validators."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "owner": {"login": owner},
                    "name": name,
                    "pullRequest": {
                        "id": f"PR_{pr_number}",
                        "number": pr_number,
                        "reviewThreads": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        },
                    },
                }
            }
        }
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
            _inline_thread_node(f"T_other_{index}", review_id="OTHER_REVIEW")
            for index in range(100)
        ]
        second_page_nodes = [
            _inline_thread_node("T_mine"),
            _inline_thread_node("T_foreign", review_id="OTHER_REVIEW"),
        ]

        def side_effect(argv: list[str], **_: Any) -> Mock:
            after_first_page = "after=cursor-1" in argv
            result = Mock(returncode=0, stderr="")
            result.stdout = _thread_page_payload(
                second_page_nodes if after_first_page else first_page_nodes,
                has_next=not after_first_page,
                end_cursor=None if after_first_page else "cursor-1",
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
        mock_result = Mock(returncode=0, stderr="")
        mock_result.stdout = _thread_page_payload([])
        mock_gh_call.return_value = mock_result

        _review_threads_for_review(42, "RV_kw1")

        argv = mock_gh_call.call_args[0][0]
        query = next(a for a in argv if a.startswith("query="))
        assert "$number:Int!" in query
        assert "pullRequest(number:$number)" in query
        assert "pullRequest(number: 42)" not in query  # regression guard
        assert 'owner: "owner"' not in query
        assert "owner=owner" in argv and "name=repo" in argv and "number=42" in argv

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    def test_review_thread_duplicates_are_returned_once(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """Identical duplicate thread payloads preserve one stable receipt ID."""
        del mock_repo_info
        node = _inline_thread_node("T1")
        result = Mock(returncode=0, stderr="")
        result.stdout = _thread_page_payload([node, node.copy()])
        mock_gh_call.return_value = result

        assert _review_threads_for_review(42, "REVIEW_1") == ["T1"]

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    def test_conflicting_duplicate_thread_ids_fail_safely(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """Conflicting duplicate IDs cannot produce an ambiguous receipt."""
        del mock_repo_info
        result = Mock(returncode=0, stderr="")
        result.stdout = _thread_page_payload(
            [_inline_thread_node("T1"), _inline_thread_node("T1", body="changed")]
        )
        mock_gh_call.return_value = result

        with pytest.raises(GraphQLDeterministicError, match="conflicting duplicate"):
            _review_threads_for_review(42, "REVIEW_1")

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info", return_value=("owner", "repo"))
    def test_graphql_errors_do_not_return_partial_review_threads(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """A later-page GraphQL error discards nodes read from earlier pages."""
        del mock_repo_info
        calls: list[list[str]] = []

        def side_effect(argv: list[str], **_: Any) -> Mock:
            calls.append(argv)
            result = Mock(returncode=0, stderr="")
            if any(entry == "after=cursor-1" for entry in argv):
                result.stdout = json.dumps({"errors": [{"message": "page failed"}]})
            else:
                result.stdout = _thread_page_payload(
                    [_inline_thread_node("T1")], has_next=True, end_cursor="cursor-1"
                )
            return result

        mock_gh_call.side_effect = side_effect

        with pytest.raises(GraphQLDeterministicError, match="page failed"):
            _review_threads_for_review(42, "REVIEW_1")
        assert len(calls) == 2
        assert "after=cursor-1" in calls[1]


class TestListUnresolvedThreadsParameterisation:
    """Tests for gh_pr_list_unresolved_threads parameterisation."""

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_uses_parameterised_query(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        mock_repo_info.return_value = ("owner", "repo")
        mock_result = Mock(returncode=0, stderr="")
        mock_result.stdout = _thread_page_payload([])
        mock_gh_call.return_value = mock_result

        gh_pr_list_unresolved_threads(42)

        argv = mock_gh_call.call_args[0][0]
        query = next(a for a in argv if a.startswith("query="))
        assert "$number:Int!" in query
        assert "pullRequest(number:$number)" in query
        assert "pullRequest(number: 42)" not in query  # regression guard
        assert 'owner: "owner"' not in query
        assert "owner=owner" in argv and "name=repo" in argv and "number=42" in argv
        # The top-level page contains only immutable thread identities; each
        # open thread is then read through its independently paginated comment
        # connection so a long conversation cannot be truncated.
        assert "nodes{id isResolved}" in query
        assert "comments(first:20)" not in query
        assert "pageInfo{hasNextPage" in query

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_rejects_missing_page_info(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """A structurally incomplete page cannot hide unresolved conversations."""
        mock_repo_info.return_value = ("owner", "repo")
        result = Mock(returncode=0, stderr="")
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "owner": {"login": "owner"},
                        "name": "repo",
                        "pullRequest": {
                            "id": "PR_42",
                            "number": 42,
                            "reviewThreads": {"nodes": []},
                        },
                    }
                }
            }
        )
        mock_gh_call.return_value = result

        with pytest.raises(GraphQLDeterministicError, match="pageInfo"):
            gh_pr_list_unresolved_threads(42)

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_rejects_missing_thread_nodes(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """A partial thread page cannot be mistaken for a proven empty set."""
        mock_repo_info.return_value = ("owner", "repo")
        result = Mock(returncode=0, stderr="")
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "owner": {"login": "owner"},
                        "name": "repo",
                        "pullRequest": {
                            "id": "PR_42",
                            "number": 42,
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None}
                            },
                        },
                    }
                }
            }
        )
        mock_gh_call.return_value = result

        with pytest.raises(GraphQLDeterministicError, match="nodes"):
            gh_pr_list_unresolved_threads(42)

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_rejects_unresolved_thread_without_comment_history(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """An open thread without a readable comment cannot be handed to an agent."""
        mock_repo_info.return_value = ("owner", "repo")

        def side_effect(argv: list[str], **_: Any) -> Mock:
            result = Mock(returncode=0, stderr="")
            if any(entry.startswith("threadId=") for entry in argv):
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {"id": "PR1", "number": 42},
                            },
                            "node": {
                                "id": "T1",
                                "isResolved": False,
                                "path": "a.py",
                                "line": 1,
                                "side": "RIGHT",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "repository": {
                                        "name": "repo",
                                        "owner": {"login": "owner"},
                                    },
                                },
                                "comments": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                            },
                        }
                    }
                )
            else:
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "reviewThreads": {
                                        "nodes": [{"id": "T1", "isResolved": False}],
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    },
                                },
                            }
                        }
                    }
                )
            return result

        mock_gh_call.side_effect = side_effect

        with pytest.raises(
            RuntimeError, match="could not fetch all comments for PR review thread T1"
        ):
            gh_pr_list_unresolved_threads(42)

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_rejects_an_unstable_thread_traversal(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """A changed second full traversal must not become an empty/clean fact."""
        mock_repo_info.return_value = ("owner", "repo")
        reads = 0

        def side_effect(_argv: list[str], **_: Any) -> Mock:
            nonlocal reads
            reads += 1
            result = Mock(returncode=0, stderr="")
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "owner": {"login": "owner"},
                            "name": "repo",
                            "pullRequest": {
                                "id": "PR1",
                                "number": 42,
                                "reviewThreads": {
                                    "nodes": [
                                        {"id": "T1" if reads == 1 else "T2", "isResolved": False}
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                            },
                        }
                    }
                }
            )
            return result

        mock_gh_call.side_effect = side_effect
        with pytest.raises(RuntimeError, match="could not stabilize all PR review threads"):
            gh_pr_list_unresolved_threads(42)

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_rejects_a_review_thread_cursor_cycle(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """A non-adjacent repeated cursor must fail rather than loop forever."""
        mock_repo_info.return_value = ("owner", "repo")

        def side_effect(argv: list[str], **_: Any) -> Mock:
            after = next(
                (entry.removeprefix("after=") for entry in argv if entry.startswith("after=")),
                "",
            )
            cursor = "cursor-a" if after == "cursor-b" else "cursor-b" if after else "cursor-a"
            result = Mock(returncode=0, stderr="")
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "owner": {"login": "owner"},
                            "name": "repo",
                            "pullRequest": {
                                "id": "PR1",
                                "number": 42,
                                "reviewThreads": {
                                    "nodes": [{"id": f"T-{after or 'first'}", "isResolved": False}],
                                    "pageInfo": {"hasNextPage": True, "endCursor": cursor},
                                },
                            },
                        }
                    }
                }
            )
            return result

        mock_gh_call.side_effect = side_effect
        with pytest.raises(RuntimeError, match="could not fetch all PR review threads"):
            gh_pr_list_unresolved_threads(42)

    @pytest.mark.parametrize(
        "exception",
        [
            pytest.param(GitHubUnavailableError("breaker open"), id="unavailable"),
            pytest.param(GitHubRateLimitError("rate limited", reset_epoch=123), id="rate-limit"),
        ],
    )
    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_preserves_provider_errors_from_thread_id_pagination(
        self,
        mock_repo_info: Any,
        mock_gh_call: Any,
        exception: RuntimeError,
    ) -> None:
        """Provider-domain errors must not be converted into pagination failures."""
        mock_repo_info.return_value = ("owner", "repo")
        mock_gh_call.side_effect = exception

        with pytest.raises(GraphQLRetryableError) as exc_info:
            gh_pr_list_unresolved_threads(42)

        assert str(exception) in str(exc_info.value)

    @patch("hephaestus.automation.github_api._gh_call")
    def test_complete_thread_snapshot_rejects_unstable_comment_pages(
        self, mock_gh_call: Any
    ) -> None:
        """A long generic thread is usable only after two matching traversals."""
        comment_reads = 0

        def side_effect(argv: list[str], **_: Any) -> Mock:
            nonlocal comment_reads
            after = "after=cursor-1" in argv
            if not after:
                comment_reads += 1
            second_pass = comment_reads == 2
            result = Mock(returncode=0, stderr="")
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "owner": {"login": "owner"},
                            "name": "repo",
                            "pullRequest": {"id": "PR1", "number": 42},
                        },
                        "node": {
                            "id": "T1",
                            "isResolved": False,
                            "path": "a.py",
                            "line": 1,
                            "side": "RIGHT",
                            "pullRequest": {
                                "id": "PR1",
                                "number": 42,
                                "repository": {"name": "repo", "owner": {"login": "owner"}},
                            },
                            "comments": {
                                "nodes": [
                                    {
                                        "id": "C3"
                                        if second_pass and after
                                        else "C2"
                                        if after
                                        else "C1",
                                        "body": "changed" if second_pass and after else "reply",
                                        "author": {"login": "reviewer"},
                                    }
                                ],
                                "pageInfo": {
                                    "hasNextPage": not after,
                                    "endCursor": None if after else "cursor-1",
                                },
                            },
                        },
                    }
                }
            )
            return result

        mock_gh_call.side_effect = side_effect
        assert _complete_thread_snapshot("owner", "repo", 42, "T1") is None

    @patch("hephaestus.automation.github_api._gh_call")
    def test_complete_thread_snapshot_rejects_duplicate_comment_ids(
        self, mock_gh_call: Any
    ) -> None:
        """A replayed or overlapping page is not a complete conversation fact."""
        result = Mock(returncode=0, stderr="")
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "owner": {"login": "owner"},
                        "name": "repo",
                        "pullRequest": {"id": "PR1", "number": 42},
                    },
                    "node": {
                        "id": "T1",
                        "isResolved": False,
                        "path": "a.py",
                        "line": 1,
                        "side": "RIGHT",
                        "pullRequest": {
                            "id": "PR1",
                            "number": 42,
                            "repository": {"name": "repo", "owner": {"login": "owner"}},
                        },
                        "comments": {
                            "nodes": [
                                {"id": "C1", "body": "first", "author": {"login": "reviewer"}},
                                {"id": "C1", "body": "duplicate", "author": {"login": "reviewer"}},
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                }
            }
        )
        mock_gh_call.return_value = result
        assert _complete_thread_snapshot("owner", "repo", 42, "T1") is None

    @pytest.mark.parametrize(
        "exception",
        [
            pytest.param(GitHubUnavailableError("breaker open"), id="unavailable"),
            pytest.param(GitHubRateLimitError("rate limited", reset_epoch=123), id="rate-limit"),
        ],
    )
    @patch("hephaestus.automation.github_api._gh_call")
    def test_preserves_provider_errors_from_comment_pagination(
        self,
        mock_gh_call: Any,
        exception: RuntimeError,
    ) -> None:
        """Provider-domain errors from comment pagination keep their original type."""
        mock_gh_call.side_effect = exception

        with pytest.raises(GraphQLRetryableError) as exc_info:
            _complete_thread_snapshot("owner", "repo", 42, "T1")

        assert str(exception) in str(exc_info.value)

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_paginates_all_review_threads_before_returning_facts(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """Threads after GitHub's first hundred remain visible to reconciliation."""
        mock_repo_info.return_value = ("owner", "repo")

        def thread(thread_id: str) -> dict[str, Any]:
            return {
                "id": thread_id,
                "isResolved": False,
            }

        first_page = [thread(f"T{index}") for index in range(100)]
        second_page = [thread("T100")]

        def side_effect(argv: list[str], **_: Any) -> Mock:
            thread_id = next(
                (
                    entry.removeprefix("threadId=")
                    for entry in argv
                    if entry.startswith("threadId=")
                ),
                None,
            )
            after_first_page = "after=cursor-1" in argv
            result = Mock(returncode=0, stderr="")
            if thread_id is not None:
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {"id": "PR1", "number": 42},
                            },
                            "node": {
                                "id": thread_id,
                                "isResolved": False,
                                "path": "a.py",
                                "line": 1,
                                "side": "RIGHT",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "repository": {
                                        "name": "repo",
                                        "owner": {"login": "owner"},
                                    },
                                },
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": f"C-{thread_id}",
                                            "body": thread_id,
                                            "author": {"login": "ci-bot"},
                                        }
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                            },
                        }
                    }
                )
            else:
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "reviewThreads": {
                                        "nodes": second_page if after_first_page else first_page,
                                        "pageInfo": {
                                            "hasNextPage": not after_first_page,
                                            "endCursor": None if after_first_page else "cursor-1",
                                        },
                                    },
                                },
                            }
                        }
                    }
                )
            return result

        mock_gh_call.side_effect = side_effect

        assert [thread["id"] for thread in gh_pr_list_unresolved_threads(42)] == [
            *(f"T{index}" for index in range(100)),
            "T100",
        ]
        list_calls = [
            call.args[0]
            for call in mock_gh_call.call_args_list
            if not any(entry.startswith("threadId=") for entry in call.args[0])
        ]
        assert len(list_calls) == 4
        assert "after=cursor-1" in list_calls[1]
        assert "after=cursor-1" in list_calls[3]

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_paginates_every_comment_before_returning_thread_facts(
        self, mock_repo_info: Any, mock_gh_call: Any
    ) -> None:
        """A human reply after one hundred bot turns remains visible to callers."""
        mock_repo_info.return_value = ("owner", "repo")
        all_comments = [
            {
                "id": f"C{index}",
                "body": f"bot reply {index}",
                "author": {"login": "ci-bot"},
            }
            for index in range(100)
        ]
        all_comments.append({"id": "C100", "body": "human reply", "author": {"login": "reviewer"}})

        def side_effect(argv: list[str], **_: Any) -> Mock:
            after = "after=comment-cursor-100" in argv
            result = Mock(returncode=0, stderr="")
            if any(entry.startswith("threadId=") for entry in argv):
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {"id": "PR1", "number": 42},
                            },
                            "node": {
                                "id": "T1",
                                "isResolved": False,
                                "path": "a.py",
                                "line": 1,
                                "side": "RIGHT",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "repository": {
                                        "name": "repo",
                                        "owner": {"login": "owner"},
                                    },
                                },
                                "comments": {
                                    "nodes": all_comments[100:] if after else all_comments[:100],
                                    "pageInfo": {
                                        "hasNextPage": not after,
                                        "endCursor": None if after else "comment-cursor-100",
                                    },
                                },
                            },
                        }
                    }
                )
            else:
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "reviewThreads": {
                                        "nodes": [{"id": "T1", "isResolved": False}],
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    },
                                },
                            }
                        }
                    }
                )
            return result

        mock_gh_call.side_effect = side_effect

        threads = gh_pr_list_unresolved_threads(42)

        assert [comment["body"] for comment in threads[0]["comments"]] == [
            *(f"bot reply {index}" for index in range(100)),
            "human reply",
        ]

    @patch("hephaestus.automation.github_api._gh_call")
    @patch("hephaestus.automation.github_api.get_repo_info")
    def test_surfaces_comment_author_login(self, mock_repo_info: Any, mock_gh_call: Any) -> None:
        """Each unresolved thread dict carries the first comment's author login."""
        mock_repo_info.return_value = ("owner", "repo")
        nodes = [
            {
                "id": "T_bot",
                "isResolved": False,
            },
            {
                "id": "T_human",
                "isResolved": False,
            },
            {
                "id": "T_noauthor",
                "isResolved": False,
            },
        ]

        def side_effect(argv: list[str], **_: Any) -> Mock:
            thread_id = next(
                (
                    entry.removeprefix("threadId=")
                    for entry in argv
                    if entry.startswith("threadId=")
                ),
                None,
            )
            result = Mock(returncode=0, stderr="")
            if thread_id is None:
                result.stdout = json.dumps(
                    {
                        "data": {
                            "repository": {
                                "owner": {"login": "owner"},
                                "name": "repo",
                                "pullRequest": {
                                    "id": "PR1",
                                    "number": 42,
                                    "reviewThreads": {
                                        "nodes": nodes,
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    },
                                },
                            }
                        }
                    }
                )
                return result
            details = {
                "T_bot": (
                    "a.py",
                    3,
                    [
                        {"id": "C1", "body": "nit", "author": {"login": "coderabbitai[bot]"}},
                        {"id": "C2", "body": "reply", "author": {"login": "mvillmow"}},
                    ],
                ),
                "T_human": (
                    "b.py",
                    None,
                    [{"id": "C3", "body": "hmm", "author": {"login": "alice"}}],
                ),
                "T_noauthor": ("c.py", 1, [{"id": "C4", "body": "x", "author": None}]),
            }
            path, line, comments = details[thread_id]
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "owner": {"login": "owner"},
                            "name": "repo",
                            "pullRequest": {"id": "PR1", "number": 42},
                        },
                        "node": {
                            "id": thread_id,
                            "isResolved": False,
                            "path": path,
                            "line": line,
                            "side": "RIGHT",
                            "pullRequest": {
                                "id": "PR1",
                                "number": 42,
                                "repository": {
                                    "name": "repo",
                                    "owner": {"login": "owner"},
                                },
                            },
                            "comments": {
                                "nodes": comments,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            },
                        },
                    }
                }
            )
            return result

        mock_gh_call.side_effect = side_effect

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
