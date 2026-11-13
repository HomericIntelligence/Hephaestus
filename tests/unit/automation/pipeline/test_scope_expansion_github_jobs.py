"""Scope-expansion contracts stay stable next to the decomposition jobs (#2982).

The decomposition worker family must not change the scope-expansion mutation
and recovery behavior. The scope-retraction helper contracts, the
current-plan scope read validation, and the decomposition receipt shapes
pin that boundary: decomposition evidence never mixes with scope evidence.
"""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    CurrentPlanScopeRead,
    DecompositionChildrenCreated,
    DecompositionChildrenRequest,
    FrozenJson,
    ReadCurrentPlanScopeRequest,
)
from hephaestus.automation.pipeline.scope_retraction import (
    SCOPE_RETRACTION_MARKER_PREFIX,
    is_safe_scope_retraction_path,
    is_scope_retraction_finding,
    normalize_scope_retraction_paths,
    scope_retraction_marker,
    scope_retraction_paths_for_threads,
    scope_retraction_paths_from_body,
)


class TestScopeRetractionHelperContracts:
    """The shared scope-retraction helper contracts are unchanged."""

    def test_scope_finding_recognition_requires_action_and_boundary(self) -> None:
        """Only a finding with one action and one scope boundary qualifies."""
        assert is_scope_retraction_finding("Drop the unrelated helper.")
        assert is_scope_retraction_finding("Remove this out-of-scope module.")
        assert not is_scope_retraction_finding("Drop the helper.")
        assert not is_scope_retraction_finding("The unrelated helper is slow.")
        assert not is_scope_retraction_finding(None)

    def test_safe_path_contract_rejects_unsafe_pathspecs(self) -> None:
        """One path contract guards both review evidence and commit pathspecs."""
        assert is_safe_scope_retraction_path("hephaestus/utils/helpers.py")
        assert not is_safe_scope_retraction_path("")
        assert not is_safe_scope_retraction_path(".")
        assert not is_safe_scope_retraction_path("../escape.py")
        assert not is_safe_scope_retraction_path("/absolute.py")
        assert not is_safe_scope_retraction_path("back`tick.py")
        assert not is_safe_scope_retraction_path(None)

    def test_complete_manifest_normalizes_to_sorted_unique_paths(self) -> None:
        """The manifest is all-or-nothing and order-stable."""
        manifest = ("b.py", "a.py")
        assert normalize_scope_retraction_paths(manifest) == ("a.py", "b.py")
        assert normalize_scope_retraction_paths(("a.py", "a.py")) == ("a.py",)
        assert normalize_scope_retraction_paths([]) is None
        assert normalize_scope_retraction_paths(("unsafe/../path",)) is None

    def test_marker_round_trips_through_the_body_reader(self) -> None:
        """One exact marker supplies the complete manifest; anything else is None."""
        marker = scope_retraction_marker(("a.py", "b.py"))
        assert marker.startswith(SCOPE_RETRACTION_MARKER_PREFIX)
        finding = f"Drop the unrelated helper.\n\n{marker}"
        assert scope_retraction_paths_from_body(finding) == ("a.py", "b.py")
        # Ordinary remediation carries no scope manifest.
        assert scope_retraction_paths_from_body("Fix the guard clause.") == ()
        # A scope request without a complete manifest stops publication.
        assert scope_retraction_paths_from_body("Drop the unrelated helper.") is None
        assert (
            scope_retraction_paths_from_body("Drop the unrelated helper.\n\nbroken manifest")
            is None
        )

    def test_thread_manifest_requires_the_anchored_file(self) -> None:
        """Each scope thread must include its anchored file in the manifest."""
        thread_body = f"Drop the unrelated helper.\n\n{scope_retraction_marker(('a.py', 'b.py'))}"
        threads = [{"path": "a.py", "body": thread_body}]
        assert scope_retraction_paths_for_threads(threads) == ("a.py", "b.py")
        assert scope_retraction_paths_for_threads([]) == ()
        anchored_outside = [{"path": "c.py", "body": thread_body}]
        assert scope_retraction_paths_for_threads(anchored_outside) is None


class TestScopeReadValidation:
    """The current-plan scope read keeps its sorted-unique path contract."""

    def test_current_plan_scope_read_rejects_unsorted_paths(self) -> None:
        request = ReadCurrentPlanScopeRequest("org/repo", 5, 30.0)
        with pytest.raises(ValueError, match="sorted unique"):
            CurrentPlanScopeRead(request, ("b.py", "a.py"), "a" * 64)
        with pytest.raises(ValueError, match="sorted unique"):
            CurrentPlanScopeRead(request, ("a.py", "a.py"), "a" * 64)
        assert CurrentPlanScopeRead(request, ("a.py", "b.py"), "a" * 64).paths == (
            "a.py",
            "b.py",
        )


class TestDecompositionKeepsScopeBoundary:
    """Decomposition receipts carry no scope-retraction evidence."""

    def test_decomposition_receipts_have_no_scope_fields(self) -> None:
        """The decomposition receipt family stays free of scope evidence."""
        from dataclasses import fields

        receipt_fields = {field.name for field in fields(DecompositionChildrenCreated)}
        assert "paths" not in receipt_fields
        assert "scope" not in " ".join(receipt_fields)
        assert not hasattr(DecompositionChildrenCreated, "scope_paths")

    def test_decomposition_request_contract_is_scope_free(self) -> None:
        """The child-creation request carries only the sealed tree identity."""
        from dataclasses import fields

        request_fields = {field.name for field in fields(DecompositionChildrenRequest)}
        assert request_fields == {
            "repository",
            "issue_number",
            "deadline_s",
            "tree",
            "requirements_epoch",
            "layers",
        }

    def test_frozen_json_snapshot_is_the_only_worker_boundary(self) -> None:
        """Worker data crosses the boundary only through canonical FrozenJson."""
        snapshot = FrozenJson.snapshot([{"node_id": "node-a"}, {"node_id": "node-b"}])
        assert snapshot.thaw() == [{"node_id": "node-a"}, {"node_id": "node-b"}]
        source: list[dict[str, str]] = [{"key": "value"}]
        detached = FrozenJson.snapshot(source)
        source[0]["key"] = "mutated"
        assert detached.thaw() == [{"key": "value"}]
