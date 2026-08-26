"""Behavior tests for portable pull-request evidence console commands."""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.github.skill_pr_review import (
    _git_read_environment,
    collect_pr_evidence_main,
    pr_diff_context_main,
    repository_from_pr_url,
    resolve_pr_main,
    validate_pr_identifier,
)


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Construct a completed subprocess result for adapter mocks."""
    return subprocess.CompletedProcess(["tool"], returncode, stdout=stdout, stderr=stderr)


def test_pr_identifier_validation_accepts_canonical_values_and_rejects_other_text() -> None:
    """PR helpers accept only an integer or a canonical GitHub PR URL."""
    validate_pr_identifier("42")
    validate_pr_identifier("https://github.com/owner/repository/pull/42")

    try:
        validate_pr_identifier("owner/repository#42")
    except RuntimeError as error:
        assert "invalid pull-request identifier" in str(error)
    else:  # pragma: no cover - defensive assertion for an unexpectedly permissive regex
        raise AssertionError("invalid PR identifier was accepted")


def test_repository_from_pr_url_rejects_an_unexpected_number() -> None:
    """Server-returned URLs must agree with the server-returned PR number."""
    try:
        repository_from_pr_url("https://github.com/owner/repository/pull/41", 42)
    except RuntimeError as error:
        assert "invalid pull-request URL" in str(error)
    else:  # pragma: no cover - defensive assertion for an invalid URL accepted
        raise AssertionError("mismatched PR URL was accepted")


def test_git_read_environment_rejects_inherited_git_overrides(monkeypatch) -> None:
    """Immutable review reads cannot inherit a caller-controlled Git graph."""
    hostile_environment = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/attacker/objects",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.commitGraph",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_DIR": "/attacker/.git",
        "GIT_GRAFT_FILE": "/attacker/grafts",
        "GIT_REPLACE_REF_BASE": "/attacker/replace",
        "GIT_WORK_TREE": "/attacker/worktree",
    }
    for key, value in hostile_environment.items():
        monkeypatch.setenv(key, value)

    environment = _git_read_environment()

    for key, value in hostile_environment.items():
        assert environment.get(key) != value
    assert environment["GIT_GRAFT_FILE"] == os.devnull
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_git_read_environment_is_a_named_minimal_allowlist(monkeypatch) -> None:
    """Unrelated parent variables and secrets cannot leak to Git read children."""
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-forward")
    monkeypatch.setenv("HEPH_GH_TIMEOUT", "poison")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = _git_read_environment()

    assert environment["PATH"] == "/usr/bin"
    assert "UNRELATED_SECRET" not in environment
    assert "HEPH_GH_TIMEOUT" not in environment


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_resolve_pr_emits_open_pr_metadata_after_repository_identity_check(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """Explicit resolution validates the supplied target and immutable revisions."""
    pull_request = {
        "number": 42,
        "url": "https://github.com/owner/repository/pull/42",
        "state": "OPEN",
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": "a" * 40,
        "baseRefOid": "b" * 40,
    }
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(pull_request)),
    ]

    assert (
        resolve_pr_main(
            ["--target-host", "github.com", "--target-repository", "owner/repository", "42"]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["review_target"] == {
        "host": "github.com",
        "kind": "github",
        "number": 42,
        "repository": "owner/repository",
        "url": "https://github.com/owner/repository/pull/42",
    }
    assert {key: value for key, value in payload.items() if key != "review_target"} == pull_request
    assert mock_gh_call.call_args_list[0].args[0][:3] == ["pr", "view", "42"]


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_resolve_pr_reports_foreign_pr_as_an_operational_error(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """A current checkout must not accidentally review a foreign pull request."""
    mock_gh_call.side_effect = [
        _completed(
            stdout=json.dumps(
                {
                    "number": 42,
                    "url": "https://github.com/other/repository/pull/42",
                    "state": "OPEN",
                    "headRefName": "feature",
                    "baseRefName": "main",
                    "headRefOid": "a" * 40,
                    "baseRefOid": "b" * 40,
                }
            )
        ),
    ]

    assert (
        resolve_pr_main(
            ["--target-host", "github.com", "--target-repository", "owner/repository", "42"]
        )
        == 1
    )

    assert "does not belong to target repository" in capsys.readouterr().err


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
def test_resolve_pr_rejects_numeric_identifier_without_explicit_target(
    _mock_throttle: MagicMock, capsys
) -> None:
    """A numeric PR never inherits repository identity from the checkout."""
    with pytest.raises(SystemExit, match="2"):
        resolve_pr_main(["42"])

    assert "require --target-host and --target-repository" in capsys.readouterr().err


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_resolve_pr_rejects_non_immutable_provider_revisions(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """The retained review target includes immutable commit OIDs only."""
    mock_gh_call.return_value = _completed(
        stdout=json.dumps(
            {
                "number": 42,
                "url": "https://github.com/owner/repository/pull/42",
                "state": "OPEN",
                "headRefName": "feature",
                "baseRefName": "main",
                "headRefOid": "main",
                "baseRefOid": "base",
            }
        )
    )

    assert (
        resolve_pr_main(
            [
                "--target-host",
                "github.com",
                "--target-repository",
                "owner/repository",
                "42",
            ]
        )
        == 1
    )

    assert "lowercase 40-hex Git commit OID" in capsys.readouterr().err


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_collect_evidence_keeps_pending_checks_and_paginated_paths(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """Evidence combines bounded metadata with the REST changed-file inventory."""
    metadata = {
        "number": 9,
        "title": "Feature",
        "body": "",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "author"},
        "baseRefName": "main",
        "headRefName": "feature",
        "reviews": [],
        "statusCheckRollup": [],
        "closingIssuesReferences": [],
        "url": "https://github.com/owner/repository/pull/9",
    }
    changed_file_pages = [
        [
            {"filename": "src/feature.py"},
            {"filename": "docs/feature.md"},
        ],
    ]
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(metadata)),
        _completed(stdout=json.dumps({"nameWithOwner": "owner/repository"})),
        _completed(stdout=json.dumps(changed_file_pages)),
        _completed(stdout=json.dumps([{"name": "checks", "state": "PENDING"}]), returncode=8),
    ]

    assert collect_pr_evidence_main(["9"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["changed_files"] == ["src/feature.py", "docs/feature.md"]
    assert payload["changed_paths"] == payload["changed_files"]
    assert payload["checks"] == [{"name": "checks", "state": "PENDING"}]
    assert "--slurp" in mock_gh_call.call_args_list[2].args[0]


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_collect_evidence_preserves_newline_in_changed_filename(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """Changed-path evidence keeps a single filename containing a newline intact."""
    metadata = {
        "number": 9,
        "title": "Feature",
        "body": "",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "author"},
        "baseRefName": "main",
        "headRefName": "feature",
        "reviews": [],
        "statusCheckRollup": [],
        "closingIssuesReferences": [],
        "url": "https://github.com/owner/repository/pull/9",
    }
    filename = "docs/line\nbreak.md"
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(metadata)),
        _completed(stdout=json.dumps({"nameWithOwner": "owner/repository"})),
        _completed(stdout=json.dumps([[{"filename": filename}]])),
        _completed(stdout=json.dumps([])),
    ]

    assert collect_pr_evidence_main(["9"]) == 0

    assert json.loads(capsys.readouterr().out)["changed_files"] == [filename]


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_collect_evidence_rejects_partial_metadata_as_json(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """Evidence collection rejects a response missing requested review fields."""
    metadata = {
        "number": 9,
        "title": "Feature",
        "state": "OPEN",
        "isDraft": False,
        "author": {"login": "author"},
        "baseRefName": "main",
        "headRefName": "feature",
        "reviews": [],
        "statusCheckRollup": [],
        "closingIssuesReferences": [],
        "url": "https://github.com/owner/repository/pull/9",
    }
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(metadata)),
        _completed(stdout=json.dumps({"nameWithOwner": "owner/repository"})),
        _completed(stdout=json.dumps([[{"filename": "src/feature.py"}]])),
        _completed(stdout=json.dumps([])),
    ]

    assert collect_pr_evidence_main(["9", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "incomplete PR metadata"
    assert "body" in payload["details"]


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_collect_evidence_reports_github_failures_as_json(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """Evidence collection keeps GitHub failures machine-readable."""
    mock_gh_call.return_value = _completed(stderr="authentication required", returncode=1)

    assert collect_pr_evidence_main(["9", "--json"]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "exit_code": 1,
        "message": "authentication required",
        "status": "error",
    }


@patch("hephaestus.github.skill_pr_review.run_git")
def test_diff_context_uses_the_supplied_base_for_both_lenses(
    mock_run_git: MagicMock, capsys
) -> None:
    """The current-base and author-intent ranges share the verified inputs."""
    base = "a" * 40
    head = "b" * 40
    merge_base = "c" * 40
    mock_run_git.side_effect = [
        _completed(stdout="false\n"),
        _completed(),
        _completed(),
        _completed(stdout=f"{merge_base}\n"),
        _completed(stdout="3\n"),
    ]

    assert pr_diff_context_main([base, head]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "base_ref": base,
        "head_ref": head,
        "merge_base": merge_base,
        "behind_count": 3,
        "author_intent_range": f"{merge_base}...{head}",
        "current_base_range": f"{base}..{head}",
    }
    first_call = mock_run_git.call_args_list[0]
    assert first_call.args[0] == [
        "-c",
        "core.commitGraph=false",
        "--no-replace-objects",
        "rev-parse",
        "--is-shallow-repository",
    ]
    assert first_call.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"


@patch("hephaestus.github.skill_pr_review.run_git")
def test_diff_context_reports_invalid_refs_as_json(mock_run_git: MagicMock, capsys) -> None:
    """Diff context keeps invalid Git references machine-readable."""
    mock_run_git.return_value = _completed(stderr="unknown revision", returncode=128)

    assert pr_diff_context_main(["a" * 40, "b" * 40, "--json"]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "exit_code": 1,
        "message": "unknown revision",
        "status": "error",
    }


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
def test_resolve_pr_json_error_is_machine_readable(_mock_throttle: MagicMock, capsys) -> None:
    """The standard --json error contract is available for orchestrators."""
    assert (
        resolve_pr_main(
            [
                "--target-host",
                "github.com",
                "--target-repository",
                "owner/repository",
                "invalid",
                "--json",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "invalid pull-request identifier" in payload["message"]


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_resolve_pr_rejects_missing_immutable_refs_as_json(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """PR resolution never succeeds without its immutable base and head references."""
    pull_request = {
        "number": 42,
        "url": "https://github.com/owner/repository/pull/42",
        "state": "OPEN",
        "headRefName": "feature",
        "baseRefName": "main",
    }
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(pull_request)),
    ]

    assert (
        resolve_pr_main(
            [
                "--target-host",
                "github.com",
                "--target-repository",
                "owner/repository",
                "42",
                "--json",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "headRefOid" in payload["message"]
