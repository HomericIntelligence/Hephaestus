"""Tests for scope-expansion GitHub reads."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import hephaestus.automation.github_api as github_api
import hephaestus.automation.pipeline_github_transport as github_transport
from hephaestus.automation.pipeline_github import PipelineGitHub


@pytest.fixture(autouse=True)
def _deny_github_calls(monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, Mock]]:
    """Reject GitHub calls unless a test supplies its own mock."""
    mocks: dict[str, Mock] = {}
    for target, name in (
        (PipelineGitHub, "_gh"),
        (PipelineGitHub, "_graphql"),
        (PipelineGitHub, "pull_request_reviews"),
        (github_transport, "gh_call"),
    ):
        boundary = Mock(name=name, side_effect=AssertionError(f"Unexpected GitHub call: {name}"))
        monkeypatch.setattr(target, name, boundary)
        mocks[name] = boundary

    yield mocks

    for boundary in mocks.values():
        boundary.assert_not_called()


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

    issues = PipelineGitHub(
        "org", repo="repo", gh_timeout=30, command_runner=fake_gh_call
    ).all_repo_issues()

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

    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    evidence = PipelineGitHub(
        "org", repo="repo", gh_timeout=30, command_runner=fake_gh_call
    ).merged_scope_expansion_pr(41)

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

    evidence = PipelineGitHub(
        "org", repo="repo", command_runner=fake_gh_call
    ).merged_scope_expansion_pr(41)

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

    with pytest.raises(RuntimeError, match="association is malformed"):
        PipelineGitHub("org", repo="repo", command_runner=fake_gh_call).merged_scope_expansion_pr(
            41
        )


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

    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    with pytest.raises(RuntimeError, match="multiple implementation"):
        PipelineGitHub("org", repo="repo", command_runner=fake_gh_call).merged_scope_expansion_pr(
            41
        )


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

    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    with pytest.raises(RuntimeError, match=expected_error):
        PipelineGitHub("org", repo="repo", command_runner=fake_gh_call).merged_scope_expansion_pr(
            41
        )


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

    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    assert (
        PipelineGitHub("org", repo="repo", command_runner=fake_gh_call).merged_scope_expansion_pr(
            41
        )
        is None
    )


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

    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    assert PipelineGitHub(
        "org", repo="repo", command_runner=fake_gh_call
    ).merged_scope_expansion_pr(41) == {
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

    monkeypatch.setattr(PipelineGitHub, "_gh", fake_gh)

    evidence = PipelineGitHub(
        "org", repo="repo", command_runner=fake_gh_call
    ).merged_scope_expansion_pr(
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


@pytest.mark.parametrize("state", ["", "opened", "OPEN", "closed "])
def test_repo_issues_rejects_invalid_state(state: str) -> None:
    """Issue discovery accepts only the documented GitHub state values."""
    with pytest.raises(ValueError, match="issue state"):
        PipelineGitHub("org", repo="repo")._repo_issues(state)


def test_repo_issues_requires_repository_scope() -> None:
    """Organization-only adapters cannot start repository issue discovery."""
    with pytest.raises(RuntimeError, match="repo-scoped"):
        PipelineGitHub("org")._repo_issues("all")


@pytest.mark.parametrize("payload", [{}, [None]])
def test_repo_issues_rejects_malformed_pages(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """Issue discovery rejects a non-list page and non-object page entries."""
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="issue list is malformed"):
        PipelineGitHub("org", repo="repo")._repo_issues("all")


def test_repo_issues_stops_at_pagination_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full hundred-page issue traversal stops at its explicit safety bound."""
    full_page: list[dict[str, object]] = [{"pull_request": {}}] * 100
    call_count = 0

    def fake_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(full_page), stderr="")

    monkeypatch.setattr(github_transport, "gh_call", fake_call)
    with pytest.raises(RuntimeError, match="traversal exceeded"):
        PipelineGitHub("org", repo="repo")._repo_issues("all")
    assert call_count == 100


@pytest.mark.parametrize("number", [True, 0, -1, "41", None])
def test_marker_issue_rejects_malformed_identity(
    monkeypatch: pytest.MonkeyPatch, number: object
) -> None:
    """A marker match needs a positive integer issue identity."""
    marker = "<!-- marker -->"
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(github, "all_repo_issues", lambda: [{"number": number, "body": marker}])
    with pytest.raises(RuntimeError, match="identity is malformed"):
        github.issues_with_marker(marker)


def test_marker_issue_rejects_body_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker issue that changes during owner proof cannot be returned."""
    marker = "<!-- marker -->"
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(github, "all_repo_issues", lambda: [{"number": 41, "body": marker}])
    monkeypatch.setattr(
        github,
        "_graphql",
        lambda spec, **fields: {"body": marker + " changed", "viewer_did_author": True},
    )
    with pytest.raises(RuntimeError, match="changed during ownership"):
        github.issues_with_marker(marker)


def test_issue_with_marker_handles_absent_and_duplicate_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unique-marker lookup reports absence and rejects duplicate ownership."""
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(github, "issues_with_marker", lambda marker: [])
    assert github.issue_with_marker("marker") is None
    monkeypatch.setattr(github, "issues_with_marker", lambda marker: [{}, {}])
    with pytest.raises(RuntimeError, match="multiple repository issues"):
        github.issue_with_marker("marker")


def test_create_issue_dry_run_returns_sentinel(_deny_github_calls: dict[str, Mock]) -> None:
    """Dry-run issue creation does not make a GitHub request."""
    assert PipelineGitHub("org", repo="repo", dry_run=True).create_issue("Title", "Body") == 0
    _deny_github_calls["_gh"].assert_not_called()


def test_create_issue_creates_missing_labels_and_parses_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue creation ensures missing labels and accepts a normal issue URL."""
    github = PipelineGitHub("org", repo="repo")
    created: list[str] = []
    captured_commands: list[list[str]] = []
    captured_bodies: list[str] = []

    def fake_gh(argv: list[str]) -> subprocess.CompletedProcess[str]:
        captured_commands.append(argv.copy())
        body_path = argv[argv.index("--body-file") + 1]
        captured_bodies.append(Path(body_path).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            argv, 0, stdout="https://github.com/org/repo/issues/42\n", stderr=""
        )

    monkeypatch.setattr(github, "_label_names", lambda: {"existing"})
    monkeypatch.setattr(github, "_create_label", created.append)
    monkeypatch.setattr(github, "_gh", fake_gh)

    assert github.create_issue("Title\x00", "Body", ["existing", "new"]) == 42
    assert created == ["new"]
    assert len(captured_commands) == 1
    command = captured_commands[0]
    body_path = command[command.index("--body-file") + 1]
    assert command == [
        "issue",
        "create",
        "--title",
        "Title",
        "--body-file",
        body_path,
        "--label",
        "existing",
        "--label",
        "new",
    ]
    assert captured_bodies == ["Body"]


@pytest.mark.parametrize("output", ["43", "https://github.test/issue/43"])
def test_create_issue_accepts_numeric_output_fallback(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    """Issue creation accepts the final numeric path component as a fallback."""
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github,
        "_gh",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=output, stderr=""),
    )
    assert github.create_issue("Title", "Body") == 43


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (OSError("disk"), "failed to create issue"),
        (subprocess.SubprocessError("gh"), "failed to create issue"),
        (RuntimeError("request"), "failed to create issue"),
    ],
)
def test_create_issue_translates_boundary_failures(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str
) -> None:
    """Issue creation translates body-file and GitHub boundary failures."""
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(github, "_gh", lambda argv: (_ for _ in ()).throw(failure))
    with pytest.raises(RuntimeError, match=message) as raised:
        github.create_issue("Title", "Body")
    assert raised.value.__cause__ is failure


def test_create_issue_rejects_unparseable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue creation fails when GitHub does not return a numeric identity."""
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github,
        "_gh",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout="not-an-issue", stderr=""),
    )
    with pytest.raises(RuntimeError, match="failed to parse issue number"):
        github.create_issue("Title", "Body")


@pytest.mark.parametrize("issue_number", [True, 0, -1, "41"])
def test_timeline_association_rejects_invalid_issue_number(issue_number: object) -> None:
    """Timeline association discovery needs a positive integer issue identity."""
    with pytest.raises(ValueError, match="positive issue number"):
        PipelineGitHub("org", repo="repo")._scope_expansion_timeline_prs(issue_number)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "event",
    [
        None,
        {"event": "cross-referenced", "source": {"type": "issue", "issue": None}},
        {
            "event": "cross-referenced",
            "source": {
                "type": "issue",
                "issue": {"number": 1, "repository_url": "relative", "pull_request": {}},
            },
        },
        {
            "event": "cross-referenced",
            "source": {
                "type": "issue",
                "issue": {
                    "number": 1,
                    "repository_url": "https://api.github.com/repos/org/repo",
                    "pull_request": {"url": None},
                },
            },
        },
        {
            "event": "cross-referenced",
            "source": {
                "type": "issue",
                "issue": {
                    "number": 1,
                    "repository_url": "https://api.github.com/repos/org/repo",
                    "pull_request": {"url": "http://api.github.com/repos/org/repo/pulls/1"},
                },
            },
        },
    ],
)
def test_timeline_association_rejects_additional_malformed_events(
    monkeypatch: pytest.MonkeyPatch, event: object
) -> None:
    """Timeline association rejects malformed events, URLs, and pull-request links."""
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps([event]), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match=r"timeline is malformed|association is malformed"):
        PipelineGitHub("org", repo="repo")._scope_expansion_timeline_prs(41)


def test_timeline_association_stops_at_pagination_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full hundred-page timeline traversal stops at its safety bound."""
    page = [{"event": "commented"}] * 100
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(page), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="timeline traversal exceeded"):
        PipelineGitHub("org", repo="repo")._scope_expansion_timeline_prs(41)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [{"number": 1, "head": None}],
        [{"number": True, "head": {"ref": "41-auto-impl", "repo": {"full_name": "org/repo"}}}],
        [{"number": 1, "head": {"ref": "other", "repo": {"full_name": "org/repo"}}}],
        [{"number": 1, "head": {"ref": "41-auto-impl", "repo": None}}],
        [{"number": 1, "head": {"ref": "41-auto-impl", "repo": {"full_name": "other/repo"}}}],
    ],
)
def test_canonical_branch_association_rejects_malformed_pages(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """Canonical branch discovery rejects malformed or cross-repository pull requests."""
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="pull-request list is malformed"):
        PipelineGitHub("org", repo="repo")._scope_expansion_canonical_branch_prs(41)


def test_canonical_branch_association_stops_at_pagination_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full hundred-page canonical branch traversal stops at its bound."""
    page = [_canonical_pr(number, 41) for number in range(1, 101)]
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(page), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="pull-request traversal exceeded"):
        PipelineGitHub("org", repo="repo")._scope_expansion_canonical_branch_prs(41)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "evidence is malformed"),
        ({"number": 73, "state": "MERGED"}, "merge timestamp is unavailable"),
        (
            {
                "number": 73,
                "state": "MERGED",
                "mergedAt": "",
                "baseRefName": "main",
                "mergeCommit": {"oid": "a" * 40},
            },
            "merge timestamp is unavailable",
        ),
    ],
)
def test_merge_evidence_rejects_additional_malformed_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    message: str,
) -> None:
    """Merged pull-request evidence needs an object and a timestamp."""
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github,
        "_gh",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr=""),
    )
    with pytest.raises(RuntimeError, match=message):
        github._scope_expansion_merge_evidence(73)


@pytest.mark.parametrize("issue_number", [True, 0, -1, "41"])
def test_merged_scope_expansion_rejects_invalid_child_identity(issue_number: object) -> None:
    """Merged-child lookup requires a positive integer child issue identity."""
    with pytest.raises(ValueError, match="positive issue number"):
        PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(issue_number)  # type: ignore[arg-type]


@pytest.mark.parametrize("source_pr_number", [True, 0, -1, "41"])
def test_merged_scope_expansion_rejects_invalid_source_identity(
    source_pr_number: object,
) -> None:
    """A source exclusion requires a positive integer pull-request identity."""
    with pytest.raises(ValueError, match="source pull-request identity"):
        PipelineGitHub("org", repo="repo").merged_scope_expansion_pr(
            41,
            source_pr_number=source_pr_number,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("ancestor", "descendant", "message"),
    [
        ("A" * 40, "main", "ancestor SHA"),
        ("a" * 39, "main", "ancestor SHA"),
        ("a" * 40, "MAIN", "descendant SHA"),
        ("a" * 40, "b" * 39, "descendant SHA"),
    ],
)
def test_commit_is_ancestor_rejects_invalid_identifiers(
    ancestor: str, descendant: str, message: str
) -> None:
    """Commit comparison accepts only full lowercase SHAs or literal main."""
    with pytest.raises(ValueError, match=message):
        PipelineGitHub("org", repo="repo").commit_is_ancestor(ancestor, descendant)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "ahead"}, True),
        ({"status": "identical"}, True),
        ({"status": "behind"}, False),
        ({"status": "diverged"}, False),
    ],
)
def test_commit_is_ancestor_maps_documented_compare_statuses(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected: bool
) -> None:
    """Commit comparison maps each documented GitHub status to ancestry."""
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    assert PipelineGitHub("org", repo="repo").commit_is_ancestor("a" * 40, "main") is expected


@pytest.mark.parametrize("payload", [[], {}, {"status": "unknown"}])
def test_commit_is_ancestor_rejects_malformed_response(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """Commit comparison fails when GitHub omits a documented status."""
    monkeypatch.setattr(
        github_transport,
        "gh_call",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match=r"comparison is malformed|status is unavailable"):
        PipelineGitHub("org", repo="repo").commit_is_ancestor("a" * 40, "main")


def test_blocking_review_rejects_body_without_marker(_deny_github_calls: dict[str, Mock]) -> None:
    """A blocking review body must keep its durable marker as the first line."""
    with pytest.raises(ValueError, match="must start with marker"):
        PipelineGitHub("org", repo="repo").post_scope_expansion_blocking_review(
            7, body="text", marker="<!-- marker -->"
        )
    _deny_github_calls["_gh"].assert_not_called()
    _deny_github_calls["pull_request_reviews"].assert_not_called()
    _deny_github_calls["gh_call"].assert_not_called()


def test_blocking_review_dry_run_returns_sentinel(_deny_github_calls: dict[str, Mock]) -> None:
    """Dry-run blocking review publication does not call GitHub."""
    marker = "<!-- marker -->"
    assert (
        PipelineGitHub("org", repo="repo", dry_run=True).post_scope_expansion_blocking_review(
            7, body=f"{marker}\ntext", marker=marker
        )
        == ""
    )
    _deny_github_calls["_gh"].assert_not_called()
    _deny_github_calls["pull_request_reviews"].assert_not_called()
    _deny_github_calls["gh_call"].assert_not_called()


@pytest.mark.parametrize("review_id", [None, ""])
def test_existing_blocking_review_requires_identity(
    monkeypatch: pytest.MonkeyPatch,
    review_id: object,
    _deny_github_calls: dict[str, Mock],
) -> None:
    """An existing matching review needs a nonempty GraphQL node identity."""
    marker = "<!-- marker -->"
    body = f"{marker}\ntext"
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github,
        "pull_request_reviews",
        lambda number: (
            {"state": "COMMENTED", "viewerDidAuthor": True, "body": body, "id": review_id},
        ),
    )
    with pytest.raises(RuntimeError, match="review id is unavailable"):
        github.post_scope_expansion_blocking_review(7, body=body, marker=marker)
    _deny_github_calls["_gh"].assert_not_called()
    _deny_github_calls["gh_call"].assert_not_called()


def test_existing_blocking_review_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    _deny_github_calls: dict[str, Mock],
) -> None:
    """A matching actor-owned review is returned without another publication."""
    marker = "<!-- marker -->"
    body = f"{marker}\ntext"
    github = PipelineGitHub("org", repo="repo")
    monkeypatch.setattr(
        github,
        "pull_request_reviews",
        lambda number: ({"state": "COMMENTED", "viewerDidAuthor": True, "body": body, "id": "R1"},),
    )
    assert github.post_scope_expansion_blocking_review(7, body=body, marker=marker) == "R1"
    _deny_github_calls["_gh"].assert_not_called()
    _deny_github_calls["gh_call"].assert_not_called()


@pytest.mark.parametrize(
    ("response", "readback", "message"),
    [
        ({}, (), "publication was not confirmed"),
        ({"node_id": "R2"}, (), "publication was not confirmed"),
        (
            {"id": "R2"},
            ({"state": "COMMENTED", "viewerDidAuthor": True, "body": "BODY", "id": "R3"},),
            "unexpected review id",
        ),
    ],
)
def test_blocking_review_publication_requires_matching_readback(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    readback: tuple[dict[str, object], ...],
    message: str,
) -> None:
    """A blocking review needs a returned identity and an exact actor-owned readback."""
    marker = "<!-- marker -->"
    body = f"{marker}\ntext"
    normalized = tuple(
        {**review, "body": body if review.get("body") == "BODY" else review.get("body")}
        for review in readback
    )
    github = PipelineGitHub("org", repo="repo")
    reviews = iter([(), normalized])
    monkeypatch.setattr(github, "pull_request_reviews", lambda number: next(reviews))
    monkeypatch.setattr(
        github,
        "_command_runner",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(response), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match=message):
        github.post_scope_expansion_blocking_review(7, body=body, marker=marker)


def test_blocking_review_publication_accepts_node_id_and_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node identity with an exact readback confirms blocking review publication."""
    marker = "<!-- marker -->"
    body = f"{marker}\ntext"
    github = PipelineGitHub("org", repo="repo")
    reviews = iter(
        [
            (),
            ({"state": "COMMENTED", "viewerDidAuthor": True, "body": body, "id": "R2"},),
        ]
    )
    monkeypatch.setattr(github, "pull_request_reviews", lambda number: next(reviews))
    monkeypatch.setattr(
        github,
        "_command_runner",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"node_id": "R2"}), stderr=""
        ),
    )
    assert github.post_scope_expansion_blocking_review(7, body=body, marker=marker) == "R2"


def test_timeline_association_requires_repository_scope() -> None:
    """Organization-only adapters cannot inspect an issue timeline."""
    with pytest.raises(RuntimeError, match="repo-scoped"):
        PipelineGitHub("org")._scope_expansion_timeline_prs(41)


def test_canonical_branch_association_requires_repository_scope() -> None:
    """Organization-only adapters cannot search canonical child branches."""
    with pytest.raises(RuntimeError, match="repo-scoped"):
        PipelineGitHub("org")._scope_expansion_canonical_branch_prs(41)


def test_commit_is_ancestor_requires_repository_scope() -> None:
    """Organization-only adapters cannot compare repository commits."""
    with pytest.raises(RuntimeError, match="repo-scoped"):
        PipelineGitHub("org").commit_is_ancestor("a" * 40, "main")
