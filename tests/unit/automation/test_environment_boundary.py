"""Executable contract for automation's environment-free configuration boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import hephaestus.automation as automation_package
import hephaestus.automation.loop_runner as loop_runner
import hephaestus.automation.pipeline_github as pipeline_github
import hephaestus.config.child_environments as child_environments

AUTOMATION_ROOT = Path(automation_package.__file__).parent
ENVIRONMENT_BOUNDARY = Path(child_environments.__file__)
REMOVED_CONFIGURATION_PREFIXES = ("HEPH_", "HEPHAESTUS_")
REMOVED_CONFIGURATION_NAMES = {"PROJECTS_ROOT"}


def test_automation_does_not_read_or_copy_the_ambient_environment() -> None:
    """Automation receives typed configuration instead of consulting process globals."""
    violations: list[str] = []

    for path in sorted(AUTOMATION_ROOT.rglob("*.py")):
        if path == ENVIRONMENT_BOUNDARY:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr
                in {
                    "environ",
                    "getenv",
                }
            ):
                violations.append(f"{path.relative_to(AUTOMATION_ROOT)}:{node.lineno}")

    assert violations == []


def test_process_environment_boundary_uses_only_literal_ambient_names() -> None:
    """Every admitted host read is statically enumerable by the policy validator."""
    tree = ast.parse(
        ENVIRONMENT_BOUNDARY.read_text(encoding="utf-8"),
        filename=str(ENVIRONMENT_BOUNDARY),
    )
    dynamic_reads: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        environ = node.func.value
        if (
            node.func.attr == "get"
            and isinstance(environ, ast.Attribute)
            and isinstance(environ.value, ast.Name)
            and environ.value.id == "os"
            and environ.attr == "environ"
            and (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            )
        ):
            dynamic_reads.append(node.lineno)

    assert dynamic_reads == []


def test_automation_contains_no_removed_configuration_variable_literals() -> None:
    """Removed environment configuration names cannot be quietly reintroduced."""
    violations: list[str] = []

    for path in sorted(AUTOMATION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            words = node.value.replace("`", " ").replace("$", " ").split()
            if any(
                word.startswith(REMOVED_CONFIGURATION_PREFIXES)
                or word in REMOVED_CONFIGURATION_NAMES
                for word in words
            ):
                violations.append(f"{path.relative_to(AUTOMATION_ROOT)}:{node.lineno}")

    assert violations == []


def test_loop_parser_exposes_typed_environment_replacements_with_exact_defaults() -> None:
    """Every removed tuning variable has a typed loop option with the same default."""
    args = loop_runner._build_parser().parse_args([])
    assert (args.planner_timeout, args.reviewer_timeout, args.implementer_timeout) == (
        1200,
        1200,
        1800,
    )
    assert args.address_review_timeout == 7200
    assert (args.git_message_timeout, args.poll_max_wait) == (1200, 1200)
    assert args.rate_guard_enabled is True
    assert args.rate_guard_threshold == 200
    assert args.disable_pi_automation is False
    assert args.auth_status_timeout == 10
    assert args.diff_collect_timeout == 60
    assert not hasattr(args, "auth_timeout")
    assert not hasattr(args, "diff_timeout")


@pytest.mark.parametrize("removed_flag", ["--auth-timeout", "--diff-timeout"])
def test_loop_parser_rejects_superseded_timeout_spellings(removed_flag: str) -> None:
    """Only the approved timeout flag names remain part of the loop interface."""
    with pytest.raises(SystemExit):
        loop_runner._build_parser().parse_args([removed_flag, "11"])


def test_role_model_precedence_is_role_then_global_then_constant() -> None:
    """Role-specific model selection wins over the global option and default."""
    assert loop_runner._resolve_model_option("role", "global", "default") == "role"
    assert loop_runner._resolve_model_option("", "global", "default") == "global"
    assert loop_runner._resolve_model_option("", "", "default") == "default"


def test_rate_guard_ignores_removed_environment_configuration(
    monkeypatch,
) -> None:
    """Poisoned legacy variables cannot alter default or explicit guard behavior."""
    monkeypatch.setenv("HEPHAESTUS_RATE_GUARD", "0")
    monkeypatch.setenv("HEPHAESTUS_RATE_GUARD_THRESHOLD", "999999")
    monkeypatch.setattr(pipeline_github, "rate_limit_remaining", lambda **_: (150, 1000))
    assert pipeline_github.rate_budget_ok(now_epoch=900) == (False, 105.0)
    assert pipeline_github.rate_budget_ok(now_epoch=900, enabled=False, threshold=999999) == (
        True,
        0.0,
    )


@pytest.mark.parametrize(
    "removed_flag",
    ["--advise-model", "--learn-model", "--advise-timeout", "--learn-timeout"],
)
def test_modern_loop_rejects_host_owned_no_op_flags(removed_flag: str) -> None:
    """Host-owned Athena work cannot advertise ignored provider configuration."""
    with pytest.raises(SystemExit):
        loop_runner._build_parser().parse_args([removed_flag, "ignored"])
