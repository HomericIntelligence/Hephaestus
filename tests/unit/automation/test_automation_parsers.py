"""Test the manual label command and shared queue parser controls."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

import pytest

from hephaestus.automation import ensure_state_labels, pipeline_cli
from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.cli.utils import DRY_RUN_HELP_CAVEAT, MODEL_REFERENCE_HELP

AGENT_CHOICES = ("claude", "codex", "pi", "opencode")
WORKER_CHOICES = tuple(range(1, 33))
SUPPRESS_DEFAULT = "==SUPPRESS=="
AGENT_HELP = (
    "Agent backend to invoke for model-driven steps "
    "(default: auto-detect authenticated backend, preferring claude when authenticated)"
)
JSON_HELP = "Emit machine-readable JSON output instead of human-readable text"
NO_UI_HELP = "Disable curses UI (use plain logging instead)"
THROTTLE_RATE_HELP = (
    "Global gh token-bucket refill rate in calls/sec (default: 10.0). "
    "Pass 0 to disable the global throttle."
)
THROTTLE_BURST_HELP = "Global gh token-bucket burst size (default: 30.0)."
VERSION_HELP = "show program's version number and exit"


@dataclass(frozen=True)
class ActionSpec:
    """Stable executable argparse action configuration relevant to CLI parity."""

    option_strings: tuple[str, ...]
    dest: str
    action: str
    default: Any
    required: bool
    nargs: Any
    choices: tuple[Any, ...] | None
    # Help prose is deliberately excluded from equality. It is operator-facing
    # documentation, not a parser behavior contract; pinning wording here
    # makes harmless documentation edits fail the executable test suite.
    help: str | None = field(compare=False)


def _action_spec(
    option_strings: tuple[str, ...],
    dest: str,
    action: str,
    default: Any,
    required: bool = False,
    nargs: Any = None,
    choices: tuple[Any, ...] | None = None,
    help_text: str | None = None,
) -> ActionSpec:
    """Build an expected argparse action spec with readable call sites."""
    return ActionSpec(
        option_strings=option_strings,
        dest=dest,
        action=action,
        default=default,
        required=required,
        nargs=nargs,
        choices=choices,
        help=help_text,
    )


def _dry_help(prefix: str) -> str:
    """Return the exact canonical dry-run help produced by add_dry_run_arg."""
    return f"{prefix} {DRY_RUN_HELP_CAVEAT}"


def _agent_spec() -> ActionSpec:
    """Return the common --agent action spec."""
    return _action_spec(
        ("--agent",),
        "agent",
        "_StoreAction",
        None,
        choices=AGENT_CHOICES,
        help_text=AGENT_HELP,
    )


def _max_workers_spec(help_text: str, default: int = 3) -> ActionSpec:
    """Return the common --max-workers action spec."""
    return _action_spec(
        ("--max-workers",),
        "max_workers",
        "_StoreAction",
        default,
        choices=WORKER_CHOICES,
        help_text=help_text,
    )


def _dry_run_spec(help_text: str) -> ActionSpec:
    """Return a --dry-run action spec."""
    return _action_spec(
        ("--dry-run",),
        "dry_run",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=help_text,
    )


def _prompt_dir_spec() -> ActionSpec:
    """Return the optional CLI-only prompt-overlay selector spec."""
    return _action_spec(
        ("--prompt-dir",),
        "prompt_dir",
        "_PromptDirAction",
        None,
        help_text="Optional directory layered over packaged Jinja prompt templates",
    )


def _verbose_spec(help_text: str) -> ActionSpec:
    """Return a -v/--verbose action spec."""
    return _action_spec(
        ("-v", "--verbose"),
        "verbose",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=help_text,
    )


def _no_ui_spec() -> ActionSpec:
    """Return the common --no-ui action spec."""
    return _action_spec(
        ("--no-ui",),
        "no_ui",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=NO_UI_HELP,
    )


def _github_throttle_specs() -> tuple[ActionSpec, ActionSpec]:
    """Return the GitHub global-throttle action specs."""
    return (
        _action_spec(
            ("--gh-global-rate",),
            "gh_global_rate",
            "_StoreAction",
            10.0,
            help_text=THROTTLE_RATE_HELP,
        ),
        _action_spec(
            ("--gh-global-burst",),
            "gh_global_burst",
            "_StoreAction",
            30.0,
            help_text=THROTTLE_BURST_HELP,
        ),
    )


def _gh_extra_path_root_spec() -> ActionSpec:
    """Return the explicit trusted GitHub CLI root action spec."""
    return _action_spec(
        ("--gh-extra-path-root",),
        "gh_extra_path_root",
        "_StoreAction",
        None,
    )


def _host_verification_pyxis_specs() -> tuple[ActionSpec, ...]:
    """Return the independent Linux Pyxis trust option specs."""
    return (
        _action_spec(
            ("--host-verification-pyxis-image",),
            "host_verification_pyxis_image",
            "_StoreAction",
            None,
            help_text="Owner-only Enroot squashfs path shared with Slurm compute nodes.",
        ),
        _action_spec(
            ("--host-verification-pyxis-sha256",),
            "host_verification_pyxis_sha256",
            "_StoreAction",
            None,
            help_text="Expected squashfs SHA-256 from a separate host-owned authority.",
        ),
        _action_spec(
            ("--host-verification-pyxis-authority",),
            "host_verification_pyxis_authority",
            "_StoreAction",
            None,
            help_text="Owner-only provenance authority for the expected squashfs.",
        ),
        _action_spec(
            ("--host-verification-pyxis-quota-root",),
            "host_verification_pyxis_quota_root",
            "_StoreAction",
            None,
            help_text="Private maximum-1-GiB filesystem shared with Slurm compute nodes.",
        ),
    )


def _json_spec() -> ActionSpec:
    """Return the common --json action spec."""
    return _action_spec(
        ("--json",),
        "json",
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=JSON_HELP,
    )


def _version_spec() -> ActionSpec:
    """Return the common -V/--version action spec."""
    return _action_spec(
        ("-V", "--version"),
        "version",
        "_VersionAction",
        SUPPRESS_DEFAULT,
        nargs=0,
        help_text=VERSION_HELP,
    )


def _store_true(option: str, dest: str, help_text: str) -> ActionSpec:
    """Return a single-option store_true spec."""
    return _action_spec(
        (option,),
        dest,
        "_StoreTrueAction",
        False,
        nargs=0,
        help_text=help_text,
    )


def _timeout_spec(flag: str, dest: str, help_text: str) -> ActionSpec:
    """Return a timeout integer option spec (default=None)."""
    return _action_spec(
        (flag,),
        dest,
        "_StoreAction",
        None,
        help_text=help_text,
    )


def _specs(parser: argparse.ArgumentParser) -> tuple[ActionSpec, ...]:
    """Return comparable action specs for a parser, excluding argparse help."""
    return tuple(
        ActionSpec(
            option_strings=tuple(action.option_strings),
            dest=action.dest,
            action=type(action).__name__,
            default=SUPPRESS_DEFAULT if action.default is argparse.SUPPRESS else action.default,
            required=getattr(action, "required", False),
            nargs=action.nargs,
            choices=tuple(action.choices) if action.choices is not None else None,
            help=action.help,
        )
        for action in parser._actions
        if action.option_strings != ["-h", "--help"]
    )


def _sorted_specs(specs: tuple[ActionSpec, ...]) -> list[ActionSpec]:
    """Sort specs so tests assert parser surface without pinning help order."""
    return sorted(specs, key=lambda spec: spec.option_strings)


EXPECTED_MANUAL_SPECS = (
    _action_spec(
        ("--repo",),
        "repo",
        "_StoreAction",
        None,
        help_text="Single target repo (default: the current git checkout's origin).",
    ),
    _action_spec(
        ("--org",),
        "org",
        "_StoreAction",
        None,
        help_text="Apply to every non-archived, non-fork repo in the org.",
    ),
    _dry_run_spec("Print what would happen; mutate nothing."),
    _verbose_spec("Enable DEBUG logging."),
    *_github_throttle_specs(),
    _json_spec(),
    _version_spec(),
)

_ENV_MIGRATION_ACTIONS = frozenset(
    {
        "--disable-pi-automation",
        "--auth-status-timeout",
        "--pi-isolation-adapter",
        "--pi-dir",
        "--codex-isolation-adapter",
        "--codex-isolation-deployment-lock",
        "--codex-isolation-deployment-lock-sha256",
        "--log-file",
        "--log-format",
        "-q",
        "--model",
        "--planner-agent",
        "--reviewer-agent",
        "--implementer-agent",
        "--planner-model",
        "--reviewer-model",
        "--implementer-model",
        "--advise-model",
        "--learn-model",
        "--fallback-model",
        "--projects-dir",
        "--rate-guard",
        "--no-rate-guard",
        "--rate-guard-threshold",
        "--plugin-skills-dir",
        "--agent-timeout",
        "--planner-timeout",
        "--reviewer-timeout",
        "--implementer-timeout",
        "--learn-timeout",
        "--advise-timeout",
        "--address-review-timeout",
        "--follow-up-timeout",
        "--git-message-timeout",
        "--poll-max-wait",
        "--plan-stage-timeout",
        "--git-timeout",
        "--clone-timeout",
        "--network-timeout",
        "--gh-timeout",
        "--metadata-timeout",
        "--rebase-timeout",
        "--diff-collect-timeout",
        "--pre-pr-test-timeout",
        "--run-pre-pr-tests",
        "--work-report",
    }
)


def test_manual_label_parser_keeps_its_action_contract() -> None:
    """The manual label command keeps its option contract."""
    actual = _specs(ensure_state_labels._build_parser())
    expected = (*EXPECTED_MANUAL_SPECS, _prompt_dir_spec())
    actual_by_options = {spec.option_strings: spec for spec in actual}
    migrated = tuple(
        spec
        for spec in actual
        if any(option in _ENV_MIGRATION_ACTIONS for option in spec.option_strings)
    )
    preserved = tuple(
        actual_by_options.get(spec.option_strings, spec)
        if any(option in _ENV_MIGRATION_ACTIONS for option in spec.option_strings)
        else spec
        for spec in expected
        if spec.option_strings in actual_by_options
        or not any(option in _ENV_MIGRATION_ACTIONS for option in spec.option_strings)
    )
    expected_options = {spec.option_strings for spec in preserved}
    additions = tuple(spec for spec in migrated if spec.option_strings not in expected_options)
    assert _sorted_specs(actual) == _sorted_specs((*preserved, *additions))


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
@pytest.mark.parametrize("flag", ["--learning-workers", "--learning-queue-capacity"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_learning_capacity_flags_require_positive_values(
    profile: str, flag: str, value: str
) -> None:
    """Every queue command rejects invalid learning capacity."""
    with pytest.raises(SystemExit) as error:
        pipeline_cli.parse_args([flag, value], profile=profile)
    assert error.value.code == 2


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
@pytest.mark.parametrize("flag", ["--learning-workers", "--learning-queue-capacity", "--no-learn"])
def test_learning_options_have_visible_help(profile: str, flag: str) -> None:
    """Every queue command explains its learning controls."""
    action = pipeline_cli.build_parser(profile=profile)._option_string_actions[flag]
    assert action.help not in (None, argparse.SUPPRESS)
    assert str(action.help).strip()


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
@pytest.mark.parametrize("flag", ["--update-plan", "--rebase"])
def test_manual_request_options_have_explicit_defaults_and_help(profile: str, flag: str) -> None:
    """Every queue command exposes the same manual request controls."""
    action = pipeline_cli.build_parser(profile=profile)._option_string_actions[flag]
    assert isinstance(action, argparse._StoreTrueAction)
    assert action.default is False
    assert action.help not in (None, argparse.SUPPRESS)
    assert str(action.help).strip()


def test_pre_pr_tests_help_describes_the_test_gate() -> None:
    """The help text explains when the optional test gate applies."""
    action = pipeline_cli.build_parser()._option_string_actions["--run-pre-pr-tests"]
    assert action.help is not None
    assert "without an automatic required-check profile" in action.help
    assert "Hephaestus" in action.help


def test_build_automation_parser_does_not_add_throttle_by_default() -> None:
    """The shared helper adds throttle flags only when requested."""
    flags = build_automation_parser("demo")._option_string_actions
    assert "--gh-global-rate" not in flags
    assert "--gh-global-burst" not in flags


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
def test_model_help_documents_literal_model_contract(profile: str) -> None:
    """Model flags keep the provider model and optional effort contract."""
    parser = pipeline_cli.build_parser(profile=profile)
    for name in ("model", "planner-model", "implementer-model", "reviewer-model", "fallback-model"):
        assert parser._option_string_actions[f"--{name}"].help == MODEL_REFERENCE_HELP


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
def test_role_tool_options_are_optional_and_use_supported_tools(profile: str) -> None:
    """Each role keeps an independent provider choice."""
    parser = pipeline_cli.build_parser(profile=profile)
    for role in ("planner", "implementer", "reviewer"):
        action = parser._option_string_actions[f"--{role}-agent"]
        assert action.default is None
        assert action.required is False
        assert action.choices is not None
        assert tuple(action.choices) == AGENT_CHOICES
