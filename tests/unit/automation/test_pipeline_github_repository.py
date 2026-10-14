"""Tests for repository metadata reads in the pipeline GitHub adapter."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from hephaestus.automation.github_api.graphql import GraphQLDeterministicError
from hephaestus.automation.pipeline.merge_wait_admission import (
    VerifiedRepositoryDefaultBranch,
)
from hephaestus.automation.pipeline_github import PipelineGitHub


def test_verified_default_branch_uses_the_explicit_repository_scope() -> None:
    """The accessor binds both query validation and variables to its repository."""
    expected = VerifiedRepositoryDefaultBranch(
        "HomericIntelligence",
        "Hephaestus",
        "HomericIntelligence/Hephaestus",
        "master",
    )
    response = json.dumps(
        {
            "data": {
                "repository": {
                    "owner": {"login": expected.owner},
                    "name": expected.name,
                    "nameWithOwner": expected.name_with_owner,
                    "defaultBranchRef": {"name": expected.default_branch},
                }
            }
        }
    )
    command = MagicMock(
        return_value=subprocess.CompletedProcess(
            ["gh", "api", "graphql"],
            0,
            stdout=response,
            stderr="",
        )
    )
    adapter = PipelineGitHub(
        "HomericIntelligence",
        repo="Hephaestus",
        command_runner=command,
    )

    assert adapter.verified_repository_default_branch() == expected
    command.assert_called_once()
    argv = command.call_args.args[0]
    assert argv[:2] == ["api", "graphql"]
    assert argv[argv.index("owner=HomericIntelligence") - 1] == "-f"
    assert argv[argv.index("name=Hephaestus") - 1] == "-f"


def test_verified_default_branch_requires_a_repository_scope() -> None:
    """An organization-only adapter cannot infer a repository identity."""
    adapter = PipelineGitHub("HomericIntelligence")

    with pytest.raises(RuntimeError, match="requires a repo"):
        adapter.verified_repository_default_branch()


def test_verified_default_branch_propagates_graphql_validation_failure() -> None:
    """The accessor does not replace invalid metadata with a branch guess."""
    command = MagicMock(
        return_value=subprocess.CompletedProcess(
            ["gh", "api", "graphql"],
            0,
            stdout=json.dumps({"data": {"repository": None}}),
            stderr="",
        )
    )
    adapter = PipelineGitHub(
        "HomericIntelligence",
        repo="Hephaestus",
        command_runner=command,
    )

    with pytest.raises(GraphQLDeterministicError, match="repository payload was missing"):
        adapter.verified_repository_default_branch()
