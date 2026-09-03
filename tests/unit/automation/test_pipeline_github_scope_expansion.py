"""Tests for scope-expansion GitHub reads."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

import hephaestus.automation.github_api as github_api
import hephaestus.automation.pipeline_github_scope_expansion as scope_expansion_adapter
from hephaestus.automation.pipeline_github import PipelineGitHub


def _cross_reference(
    pr_number: int,
    *,
    repository_url: str = "https://api.github.com/repos/org/repo",
) -> dict[str, object]:
    """Return one same-repository pull-request cross-reference event."""
    return {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": pr_number,
                "repository_url": repository_url,
                "pull_request": {
                    "url": f"{repository_url}/pulls/{pr_number}",
                },
            },
        },
    }


def _canonical_pr(pr_number: int, child_issue_number: int) -> dict[str, object]:
    """Return one PR on the canonical child implementation branch."""
    return {
        "number": pr_number,
        "head": {
            "ref": f"{child_issue_number}-auto-impl",
            "repo": {"full_name": "org/repo"},
        },
    }


def _merged_pr_payload(pr_number: int, merge_sha: str = "a" * 40) -> dict[str, object]:
    """Return exact GitHub merge evidence for one associated PR."""
    return {
        "number": pr_number,
        "state": "MERGED",
        "mergedAt": "2026-09-03T12:00:00Z",
        "mergeCommit": {"oid": merge_sha},
        "baseRefName": "main",
    }


def test_all_repo_issues_uses_rest_pages_and_excludes_pull_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marker discovery reads all issue pages without PR pseudo-issues."""
    calls: list[list[str]] = []
    first_page: list[dict[str, Any]] = [
        {"number": number, "title": f"issue {number}", "body": "", "state": "open"}
        for number in range(1, 100)
    ]
    first_page.append(
        {
            "number": 100,
            "title": "pull request",
            "body": "marker collision",
            "state": "open",
            "pull_request": {"url": "https://api.github.test/pulls/100"},
        }
    )
    pages = [first_page, [{"number": 101, "title": "last", "body": "", "state": "closed"}]]

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(pages.pop(0)), stderr="")

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)

    issues = PipelineGitHub("org", repo="repo", gh_timeout=30).all_repo_issues()

    assert [issue["number"] for issue in issues] == [*range(1, 100), 101]
    assert calls == [
        [
            "api",
            "--method",
            "GET",
            "repos/org/repo/issues?state=all&per_page=100&page=1",
        ],
        [
            "api",
            "--method",
            "GET",
            "repos/org/repo/issues?state=all&per_page=100&page=2",
        ],
    ]


def test_merged_scope_expansion_pr_uses_all_child_timeline_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-reference, not PR text or its branch name, proves association."""
    api_calls: list[list[str]] = []
    pages = [
        [{"event": "commented"} for _index in range(100)],
        [_cross_reference(73)],
    ]

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        api_calls.append(argv)
        payload = [] if "/pulls?" in argv[-1] else pages.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    def fake_gh(
        _self: PipelineGitHub, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert argv == [
            "pr",
            "view",
            "73",
            "--json",
            "number,state,mergedAt,mergeCommit,baseRefName",
        ]
        payload = _merged_pr_payload(73)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)
    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    evidence = PipelineGitHub("org", repo="repo", gh_timeout=30).merged_scope_expansion_pr(41)

    assert evidence == {"merge_sha": "a" * 40, "base_branch": "main"}
    assert api_calls == [
        [
            "api",
            "--method",
            "GET",
            "repos/org/repo/issues/41/timeline?per_page=100&page=1",
        ],
        [
            "api",
            "--method",
            "GET",
            "repos/org/repo/issues/41/timeline?per_page=100&page=2",
        ],
        [
            "api",
            "--method",
            "GET",
            "repos/org/repo/pulls?state=all&head=org:41-auto-impl&per_page=100&page=1",
        ],
    ]


def test_merged_scope_expansion_pr_returns_none_without_associated_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issue cross-reference is not a child implementation association."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/pulls?" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        payload = [
            {
                "event": "cross-referenced",
                "source": {
                    "type": "issue",
                    "issue": {
                        "number": 73,
                        "repository_url": "https://api.github.com/repos/org/repo",
                    },
                },
            },
            _cross_reference(
                74,
                repository_url="https://api.github.com/repos/another/repo",
            ),
        ]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)

    evidence = PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(41)

    assert evidence is None


@pytest.mark.parametrize(
    "event",
    [
        {"event": "cross-referenced", "source": None},
        {
            "event": "cross-referenced",
            "source": {"type": "issue", "issue": {"number": "73", "pull_request": {}}},
        },
        {
            "event": "cross-referenced",
            "source": {
                "type": "issue",
                "issue": {
                    "number": 73,
                    "repository_url": "https://api.github.com/repos/org/repo",
                    "pull_request": [],
                },
            },
        },
    ],
)
def test_merged_scope_expansion_pr_rejects_malformed_associations(
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, object],
) -> None:
    """Malformed cross-reference evidence cannot authorize resumption."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([event]), stderr="")

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)

    with pytest.raises(RuntimeError, match="association is malformed"):
        PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(41)


def test_merged_scope_expansion_pr_rejects_multiple_associations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conflicting timeline and canonical merged PRs are ambiguous evidence."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = [_canonical_pr(74, 41)] if "/pulls?" in argv[-1] else [_cross_reference(73)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    def fake_gh(
        _self: PipelineGitHub, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        pr_number = int(argv[2])
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_merged_pr_payload(pr_number)), stderr=""
        )

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)
    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    with pytest.raises(RuntimeError, match="multiple implementation"):
        PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(41)


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (
            {
                "number": 73,
                "state": "MERGED",
                "mergedAt": "2026-09-03T12:00:00Z",
                "mergeCommit": {"oid": "a" * 39},
                "baseRefName": "main",
            },
            "merge SHA is unavailable",
        ),
        (
            {
                "number": 73,
                "state": "MERGED",
                "mergedAt": "2026-09-03T12:00:00Z",
                "mergeCommit": {"oid": "a" * 40},
                "baseRefName": "release",
            },
            "did not merge into main",
        ),
        (
            {
                "number": 74,
                "state": "MERGED",
                "mergedAt": "2026-09-03T12:00:00Z",
                "mergeCommit": {"oid": "a" * 40},
                "baseRefName": "main",
            },
            "identity is malformed",
        ),
    ],
)
def test_merged_scope_expansion_pr_rejects_invalid_merge_evidence(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected_error: str,
) -> None:
    """The unique association needs exact main-merge evidence."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/pulls?" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([_cross_reference(73)]), stderr=""
        )

    def fake_gh(
        _self: PipelineGitHub, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)
    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    with pytest.raises(RuntimeError, match=expected_error):
        PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(41)


def test_merged_scope_expansion_pr_returns_none_for_unmerged_association(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open associated pull request is not merge evidence."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "/pulls?" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([_cross_reference(73)]), stderr=""
        )

    def fake_gh(
        _self: PipelineGitHub, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        payload = {
            "number": 73,
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
            "baseRefName": "main",
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)
    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    assert PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(41) is None


def test_merged_scope_expansion_pr_accepts_canonical_branch_without_cross_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical child branch is independent association evidence."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = [_canonical_pr(73, 41)] if "/pulls?" in argv[-1] else []
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    def fake_gh(
        _self: PipelineGitHub, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_merged_pr_payload(73)), stderr=""
        )

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)
    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    assert PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(41) == {
        "merge_sha": "a" * 40,
        "base_branch": "main",
    }


def test_merged_scope_expansion_pr_excludes_blocked_source_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source PR child link does not conflict with the child implementation."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = (
            [_canonical_pr(73, 41)]
            if "/pulls?" in argv[-1]
            else [_cross_reference(2859), _cross_reference(73)]
        )
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    def fake_gh(
        _self: PipelineGitHub, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert argv[2] == "73"
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(_merged_pr_payload(73)), stderr=""
        )

    monkeypatch.setattr(scope_expansion_adapter, "direct_gh_call", fake_gh_call)
    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    evidence = PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(
        41,
        source_pr_number=2859,
    )

    assert evidence == {"merge_sha": "a" * 40, "base_branch": "main"}


def test_issue_with_marker_ignores_forged_public_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an issue that GitHub attributes to the actor can own a marker."""
    marker = "<!-- hephaestus-scope-expansion-child:v1:abc -->"
    issues = [
        {"number": 41, "body": f"{marker}\nforged"},
        {"number": 42, "body": f"{marker}\nowned"},
    ]
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(github, "all_repo_issues", lambda: issues)

    def fake_graphql(
        spec: github_api.GraphQLQuerySpec[dict[str, object]],
        **fields: int | str,
    ) -> dict[str, object]:
        number = fields["number"]
        issue = issues[int(number) - 41]
        return spec.validate(
            {
                "repository": {
                    "owner": {"login": "org"},
                    "name": "repo",
                    "issue": {
                        "number": number,
                        "body": issue["body"],
                        "viewerDidAuthor": number == 42,
                    },
                }
            }
        )

    monkeypatch.setattr(github, "_graphql", fake_graphql)

    assert github.issue_with_marker(marker) == issues[1]
