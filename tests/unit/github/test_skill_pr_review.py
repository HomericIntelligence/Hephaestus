"""Behavior tests for portable pull-request evidence console commands."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

from hephaestus.github.skill_pr_review import (
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


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
@patch("hephaestus.github.skill_pr_review.gh_call")
def test_resolve_pr_emits_open_pr_metadata_after_repository_identity_check(
    mock_gh_call: MagicMock, _mock_throttle: MagicMock, capsys
) -> None:
    """Explicit resolution validates both the state and the current repository."""
    pull_request = {
        "number": 42,
        "url": "https://github.com/owner/repository/pull/42",
        "state": "OPEN",
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": "head",
        "baseRefOid": "base",
    }
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(pull_request)),
        _completed(stdout=json.dumps({"nameWithOwner": "owner/repository"})),
    ]

    assert resolve_pr_main(["42"]) == 0

    assert json.loads(capsys.readouterr().out) == pull_request
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
                }
            )
        ),
        _completed(stdout=json.dumps({"nameWithOwner": "owner/repository"})),
    ]

    assert resolve_pr_main(["42"]) == 1

    assert "does not belong to current repository" in capsys.readouterr().err


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
    mock_gh_call.side_effect = [
        _completed(stdout=json.dumps(metadata)),
        _completed(stdout=json.dumps({"nameWithOwner": "owner/repository"})),
        _completed(stdout="src/feature.py\ndocs/feature.md\n"),
        _completed(stdout=json.dumps([{"name": "checks", "state": "PENDING"}]), returncode=8),
    ]

    assert collect_pr_evidence_main(["9"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["changed_files"] == ["src/feature.py", "docs/feature.md"]
    assert payload["changed_paths"] == payload["changed_files"]
    assert payload["checks"] == [{"name": "checks", "state": "PENDING"}]


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
    mock_run_git.side_effect = [
        _completed(),
        _completed(),
        _completed(stdout="merge-base\n"),
        _completed(stdout="3\n"),
    ]

    assert pr_diff_context_main(["base", "head"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "base_ref": "base",
        "head_ref": "head",
        "merge_base": "merge-base",
        "behind_count": 3,
        "author_intent_range": "merge-base...head",
        "current_base_range": "base..head",
    }
    assert mock_run_git.call_args_list[0].args[0] == ["rev-parse", "--verify", "base^{commit}"]


@patch("hephaestus.github.skill_pr_review.run_git")
def test_diff_context_reports_invalid_refs_as_json(mock_run_git: MagicMock, capsys) -> None:
    """Diff context keeps invalid Git references machine-readable."""
    mock_run_git.return_value = _completed(stderr="unknown revision", returncode=128)

    assert pr_diff_context_main(["missing", "head", "--json"]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "exit_code": 1,
        "message": "unknown revision",
        "status": "error",
    }


@patch("hephaestus.github.skill_pr_review.configure_github_throttle_from_args")
def test_resolve_pr_json_error_is_machine_readable(_mock_throttle: MagicMock, capsys) -> None:
    """The standard --json error contract is available for orchestrators."""
    assert resolve_pr_main(["invalid", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "invalid pull-request identifier" in payload["message"]
