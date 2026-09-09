"""Test logging delegation through retained command entry points."""

from __future__ import annotations

import logging
from importlib import import_module
from unittest.mock import Mock, patch

import pytest

from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT


class _StopAfterLoggingError(RuntimeError):
    """Stop a command after logging setup."""


@pytest.mark.parametrize(
    ("module_name", "callable_name", "kwargs", "expected_level"),
    [
        ("hephaestus.cli.utils", "configure_cli_logging", {"verbose": False}, logging.INFO),
        ("hephaestus.cli.utils", "configure_cli_logging", {"verbose": True}, logging.DEBUG),
        (
            "hephaestus.cli.utils",
            "configure_cli_logging",
            {"verbose": False, "log_format": "json"},
            logging.INFO,
        ),
        ("hephaestus.automation.pipeline_cli", "_setup_logging", {"verbose": False}, logging.INFO),
    ],
)
def test_cli_logging_helpers_delegate_to_shared_helper(
    module_name: str,
    callable_name: str,
    kwargs: dict[str, object],
    expected_level: int,
) -> None:
    """CLI logging helpers use the common logging configuration."""
    helper = getattr(import_module(module_name), callable_name)
    with patch("hephaestus.cli.utils.setup_logging") as setup:
        helper(**kwargs)
    setup.assert_called_once_with(
        level=expected_level,
        log_file=None,
        format_string=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        primary_stream="stderr",
        json_format=kwargs.get("log_format") == "json",
    )


def test_tidy_logging_delegates_to_shared_helper() -> None:
    """Tidy logging forwards the selected explicit format to the shared helper."""
    module = import_module("hephaestus.github.tidy")

    with patch.object(module, "configure_cli_logging", Mock()) as configure:
        module._configure_logging(verbose=False, log_format="json")

    configure.assert_called_once_with(verbose=False, log_format="json")


def test_fleet_sync_main_delegates_logging_to_shared_helper() -> None:
    """Fleet sync CLI uses stderr-safe shared logging setup."""
    module = import_module("hephaestus.github.fleet_sync.cli")

    with (
        patch.object(module, "configure_github_throttle_from_args") as throttle,
        patch.object(module, "resolve_agent", return_value="claude"),
        patch.object(module, "resolve_fleet_config", return_value=("Org", [])),
        patch.object(module, "configure_cli_logging", Mock()) as configure,
    ):
        rc = module.main(["--verbose", "--log-format", "json"])

    assert rc == 0
    throttle.assert_called_once()
    configure.assert_called_once_with(verbose=True, log_format="json")


@pytest.mark.parametrize("module_name", ["loop_runner", "planner", "implementer", "pr_reviewer"])
def test_queue_commands_forward_logging_arguments(module_name: str) -> None:
    """Each queue command sends logging options through the shared parser."""
    from hephaestus.automation import pipeline_cli

    entry = import_module(f"hephaestus.automation.{module_name}")
    with (
        patch.object(pipeline_cli, "resolve_agent", side_effect=_StopAfterLoggingError()),
        patch.object(pipeline_cli, "configure_cli_logging") as configure,
        patch.object(pipeline_cli, "configure_github_throttle_from_args"),
        pytest.raises(_StopAfterLoggingError),
    ):
        entry.main(["--verbose", "--log-format", "json", "--log-file", "queue.log"])
    configure.assert_called_once_with(
        verbose=True, log_format="json", quiet=False, log_file="queue.log"
    )


def test_loop_logging_forwards_all_options() -> None:
    """Forward file, format, and level options to the common CLI helper."""
    module = import_module("hephaestus.automation.pipeline_cli")
    with patch.object(module, "configure_cli_logging") as configure:
        module._setup_logging(True, "json", quiet=True, log_file="loop.log")
    configure.assert_called_once_with(
        verbose=True,
        log_format="json",
        quiet=True,
        log_file="loop.log",
    )
