"""Regression coverage for queue-pipeline wrapper repository identity."""

from __future__ import annotations

from types import ModuleType
from unittest.mock import patch

import pytest

from hephaestus.automation import ci_driver, implementer, planner, pr_reviewer


@pytest.mark.parametrize(
    "wrapper",
    [
        pytest.param(planner, id="planner"),
        pytest.param(implementer, id="implementer"),
        pytest.param(pr_reviewer, id="pr_reviewer"),
        pytest.param(ci_driver, id="ci_driver"),
    ],
)
def test_pipeline_wrappers_use_full_repository_identity(wrapper: ModuleType) -> None:
    """Pipeline scopes use ``get_repo_info``, not the short logging slug."""
    with (
        patch.object(wrapper, "get_repo_slug", return_value="Hephaestus", create=True) as get_slug,
        patch.object(
            wrapper,
            "get_repo_info",
            return_value=("HomericIntelligence", "Hephaestus"),
            create=True,
        ) as get_repo_info,
    ):
        assert wrapper._resolve_repo() == ("HomericIntelligence", "Hephaestus")

    get_repo_info.assert_called_once_with()
    get_slug.assert_not_called()
