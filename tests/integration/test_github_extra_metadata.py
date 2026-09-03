"""Wheel metadata contracts for the retained ``github`` compatibility extra."""

from __future__ import annotations

from email.message import Message
from email.parser import BytesParser
from email.policy import default
from zipfile import ZipFile

import pytest
from packaging.requirements import Requirement

from .artifact_support import ControlledArtifacts

pytestmark = [pytest.mark.integration, pytest.mark.artifact]


def _wheel_metadata(artifacts: ControlledArtifacts) -> Message:
    """Return the wheel metadata message for the current artifact."""
    with ZipFile(artifacts.first_wheel) as wheel:
        metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1
        return BytesParser(policy=default).parsebytes(wheel.read(metadata_names[0]))


def test_github_extra_is_empty_compatibility_extra(
    controlled_artifacts: ControlledArtifacts,
) -> None:
    """The retained github extra adds no wheel dependency."""
    metadata = _wheel_metadata(controlled_artifacts)
    assert "github" in metadata.get_all("Provides-Extra", [])

    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]
    assert all(requirement.name.lower() != "pygithub" for requirement in requirements)
    assert all(
        requirement.marker is None or not requirement.marker.evaluate({"extra": "github"})
        for requirement in requirements
    )
