"""Automation keeps the remote Git contract re-exported from the library.

The behavior tests live with the owning module in
``tests/unit/github/test_remote_git.py``. This module asserts the product
layer keeps object identity for every re-exported name, so current automation
callers and monkeypatch targets stay valid.
"""

# ruff: noqa: D103
from pathlib import Path
from unittest.mock import patch

import pytest

import hephaestus.automation.remote_git as automation
import hephaestus.github.remote_git as library

_EXPORTED_NAMES = (
    "TrustedRemoteGit",
    "trusted_gh_authenticated",
    "trusted_gh_executable",
    "trusted_remote_git_config",
)


@pytest.mark.parametrize("name", _EXPORTED_NAMES)
def test_automation_reexports_keep_library_identity(name: str) -> None:
    assert getattr(automation, name) is getattr(library, name)


def test_automation_module_exports_the_full_contract() -> None:
    assert set(automation.__all__) == set(_EXPORTED_NAMES)


def test_automation_extra_root_parse_delegates_to_the_library() -> None:
    from hephaestus.automation import _review_utils

    sentinel = Path("/trusted/root")
    with patch.object(library, "validate_gh_extra_path_root", return_value=sentinel) as validate:
        assert _review_utils._parse_gh_extra_path_root("/trusted/root") == sentinel
    assert validate.call_args.args == ("/trusted/root",)


def test_automation_extra_root_arg_uses_the_library_type() -> None:
    import argparse

    from hephaestus.automation._review_utils import add_gh_extra_path_root_arg

    parser = argparse.ArgumentParser()
    add_gh_extra_path_root_arg(parser)
    argument = next(action for action in parser._actions if action.dest == "gh_extra_path_root")
    assert argument.type is library.gh_extra_path_root_type


@pytest.mark.parametrize("extra", [None, Path("/host/tools")])
def test_default_host_transports_share_host_root(extra: Path | None) -> None:
    from hephaestus.automation.mnemosyne_learning_preparation import BoundLearningWorkspace
    from hephaestus.automation.mnemosyne_skill_host import (
        DefaultLearnDeliveryBackend,
        MnemosyneSkillHost,
    )

    host = MnemosyneSkillHost(gh_extra_path_root=extra)
    backend = host.delivery_service
    assert isinstance(backend, DefaultLearnDeliveryBackend)
    assert isinstance(backend._service.remote_git, library.TrustedRemoteGit)
    assert backend._service.remote_git.extra_path_root == extra
    assert isinstance(backend._preparation._workspace, BoundLearningWorkspace)
    assert isinstance(backend._preparation._workspace._remote_git, library.TrustedRemoteGit)
    assert backend._preparation._workspace._remote_git.extra_path_root == extra


def test_library_command_resolution_runs_through_library_globals() -> None:
    sentinel = object()
    with patch.object(library, "trusted_gh_executable", return_value=sentinel):
        # Automation callers that patch the automation binding patch their own
        # reference; the library command resolution patches the library one.
        assert automation.trusted_gh_executable is not library.trusted_gh_executable
        assert library.TrustedRemoteGit is automation.TrustedRemoteGit
    assert automation.trusted_gh_executable is library.trusted_gh_executable
