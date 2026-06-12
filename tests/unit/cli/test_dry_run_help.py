"""Tests for the shared --dry-run help-text contract (#772)."""

from __future__ import annotations

import argparse
import importlib

import pytest

from hephaestus.cli.utils import DRY_RUN_HELP_CAVEAT, add_dry_run_arg

TOKEN_COST_SENTENCE = "still incurs full Claude API token cost"  # noqa: S105

CLI_PARSER_BUILDERS = [
    "hephaestus.automation.planner",
    "hephaestus.automation.plan_reviewer",
    "hephaestus.automation.pr_reviewer",
    "hephaestus.automation.address_review",
    "hephaestus.automation.implementer_cli",
    "hephaestus.automation.ci_driver",
    "hephaestus.automation.loop_runner",
]


def _dry_run_help(parser: argparse.ArgumentParser) -> str:
    for action in parser._actions:
        if "--dry-run" in action.option_strings:
            return action.help or ""
    raise AssertionError("--dry-run not found on parser")


def test_canonical_caveat_mentions_token_cost() -> None:
    """Test that canonical caveat mentions token cost."""
    assert TOKEN_COST_SENTENCE in DRY_RUN_HELP_CAVEAT


def test_add_dry_run_arg_appends_flag_with_caveat() -> None:
    """Test that add_dry_run_arg appends the flag with canonical caveat."""
    p = argparse.ArgumentParser()
    add_dry_run_arg(p)
    help_text = _dry_run_help(p)
    assert TOKEN_COST_SENTENCE in help_text
    # When no prefix is supplied, the caveat is the whole help string.
    assert help_text == DRY_RUN_HELP_CAVEAT


def test_add_dry_run_arg_prefix_precedes_caveat() -> None:
    """Test that prefix text precedes the canonical caveat."""
    p = argparse.ArgumentParser()
    add_dry_run_arg(p, prefix="No review comments posted.")
    help_text = _dry_run_help(p)
    assert help_text.startswith("No review comments posted.")
    assert help_text.endswith(DRY_RUN_HELP_CAVEAT)
    assert TOKEN_COST_SENTENCE in help_text


def test_add_dry_run_arg_prefix_without_terminal_punctuation_gets_period() -> None:
    """Guards against the 'Suppress mutations NOTE: Claude…' concatenation bug."""
    p = argparse.ArgumentParser()
    add_dry_run_arg(p, prefix="Suppress mutations")
    help_text = _dry_run_help(p)
    assert "Suppress mutations. " in help_text
    assert help_text.endswith(DRY_RUN_HELP_CAVEAT)


@pytest.mark.parametrize("module_path", CLI_PARSER_BUILDERS)
def test_every_cli_parser_carries_canonical_caveat(module_path: str) -> None:
    """Test that every CLI parser carries the canonical caveat."""
    mod = importlib.import_module(module_path)
    parser = mod._build_parser()
    assert isinstance(parser, argparse.ArgumentParser)
    help_text = _dry_run_help(parser)
    assert TOKEN_COST_SENTENCE in help_text, (
        f"{module_path}._build_parser() produced --dry-run help without "
        f"the canonical caveat: {help_text!r}"
    )
