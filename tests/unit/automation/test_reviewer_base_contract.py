"""Lock the BaseReviewer test-seam contract (see issues #806, #710)."""

from __future__ import annotations

import pytest

from hephaestus.automation import _reviewer_base, address_review, pr_reviewer


@pytest.mark.parametrize("subclass_module", [pr_reviewer, address_review])
@pytest.mark.parametrize(
    "name", ["get_repo_root", "WorktreeManager", "StatusTracker", "ThreadLogManager"]
)
def test_subclass_module_reexports_patchable_dependency(subclass_module, name):
    """Each concrete reviewer subclass must re-export every patchable dep."""
    assert hasattr(subclass_module, name), (
        f"{subclass_module.__name__} must re-export {name!r} for the "
        f"BaseReviewer test-seam contract (issue #710)."
    )


def test_patchable_dependencies_tuple_is_single_source_of_truth():
    """The class-level tuple is the documented contract — do not drift."""
    assert _reviewer_base.BaseReviewer._PATCHABLE_DEPENDENCIES == (
        "get_repo_root",
        "WorktreeManager",
        "StatusTracker",
        "ThreadLogManager",
    )


def test_missing_reexport_raises_actionable_typeerror():
    """A third subclass that forgets the contract gets a pointed error."""

    class BogusSubclass(_reviewer_base.BaseReviewer):
        pass

    with pytest.raises(TypeError, match="test-seam contract"):
        BogusSubclass(options=object())
