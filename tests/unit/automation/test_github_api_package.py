"""Package-level compatibility tests for GitHub API helpers."""

from __future__ import annotations

import importlib
import json
from unittest import mock

from hephaestus.automation import github_api as gha


def test_public_reexports_match_canonical_submodules() -> None:
    """The package shim must re-export the canonical submodule functions."""
    expected = {
        "checks": ["gh_pr_checks", "_map_pr_check"],
        "diff": ["_filter_comments_to_diff", "_valid_review_positions"],
        "issue_states": ["prefetch_issue_states", "_fetch_batch_states"],
        "issues": ["gh_issue_json", "gh_issue_create", "fetch_issue_info"],
        "labels": ["gh_list_labels", "gh_create_label", "_ensure_labels_exist"],
        "prs": ["gh_pr_create", "fetch_open_prs", "gh_current_login", "gh_pr_label_names"],
        "reviews": ["gh_pr_review_post", "gh_pr_inline_comment_index"],
        "threads": ["gh_pr_list_unresolved_threads", "gh_pr_resolve_thread"],
    }

    for module_name, names in expected.items():
        module = importlib.import_module(f"hephaestus.automation.github_api.{module_name}")
        for name in names:
            assert getattr(gha, name) is getattr(module, name)


def test_patch_on_package_reaches_internal_sibling_caller() -> None:
    """Package-level patches must reach submodule sibling calls."""
    labels = importlib.import_module("hephaestus.automation.github_api.labels")

    with (
        mock.patch.object(gha, "gh_list_labels", return_value={"existing"}) as list_labels,
        mock.patch.object(gha, "gh_create_label") as create_label,
    ):
        labels._ensure_labels_exist(["existing", "new"])

    list_labels.assert_called_once_with()
    create_label.assert_called_once_with("new")


def test_patch_gh_call_reaches_submodule() -> None:
    """Imported-through patches must be read through the package namespace."""
    labels = importlib.import_module("hephaestus.automation.github_api.labels")
    result = mock.Mock(stdout="[]")

    with mock.patch.object(gha, "_gh_call", return_value=result) as gh_call:
        assert labels.gh_list_labels(refresh=True) == set()

    gh_call.assert_called_once()


def test_gh_pr_label_names_normalizes_label_dicts() -> None:
    """gh_pr_label_names flattens gh's ``{"name": ...}`` label dicts to names."""
    payload = {"labels": [{"name": "state:implementation-go"}, {"name": "bug"}, "plain"]}
    result = mock.Mock(stdout=json.dumps(payload))

    with mock.patch.object(gha, "_gh_call", return_value=result) as gh_call:
        names = gha.gh_pr_label_names(77)

    assert names == ["state:implementation-go", "bug", "plain"]
    gh_call.assert_called_once_with(["pr", "view", "77", "--json", "labels"], check=False)


def test_gh_pr_label_names_best_effort_empty_on_failure() -> None:
    """A fetch/JSON failure yields [] (read as "not yet reviewed" by seeding)."""
    with mock.patch.object(gha, "_gh_call", side_effect=OSError("gh down")):
        assert gha.gh_pr_label_names(77) == []


def test_gh_pr_label_names_non_list_labels_yields_empty() -> None:
    """A malformed payload (labels not a list) yields [] rather than raising."""
    result = mock.Mock(stdout=json.dumps({"labels": "oops"}))
    with mock.patch.object(gha, "_gh_call", return_value=result):
        assert gha.gh_pr_label_names(77) == []
