"""Cross-cutting invariants for explicit runtime and subprocess configuration."""

from __future__ import annotations

import ast
from pathlib import Path

from hephaestus.automation import loop_runner

ROOT = Path(__file__).parents[3]
RUNTIME_ROOT = ROOT / "hephaestus"


def _production_trees() -> list[tuple[Path, ast.Module]]:
    """Return every governed runtime source and its parsed syntax tree."""
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(RUNTIME_ROOT.rglob("*.py"))
        if path.name != "_version.py"
    ]


def test_direct_subprocess_calls_always_supply_an_environment() -> None:
    """No direct subprocess invocation may inherit the complete parent mapping."""
    violations: list[str] = []
    methods = {"run", "Popen", "check_call", "check_output", "call"}

    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in methods
                and not any(keyword.arg == "env" for keyword in node.keywords)
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == []


def test_shared_subprocess_calls_always_supply_an_environment() -> None:
    """The shared helper cannot hide a generic environment fallback at call sites."""
    violations: list[str] = []

    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name == "run_subprocess" and not any(
                keyword.arg == "env" for keyword in node.keywords
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == []


def test_loop_cli_values_are_typed_and_override_defaults() -> None:
    """Representative model, timeout, rate, path, and provider values resolve once."""
    args = loop_runner._build_parser().parse_args(
        [
            "--planner-model",
            "planner-explicit",
            "--implementer-timeout",
            "1811",
            "--git-message-timeout",
            "311",
            "--gh-timeout",
            "19",
            "--gh-global-rate",
            "4.5",
            "--gh-global-burst",
            "9",
            "--rate-guard-threshold",
            "321",
            "--log-format",
            "json",
            "--pi-isolation-adapter",
            "operator-broker",
            "--pi-dir",
            "/tmp/explicit-pi",
        ]
    )

    assert args.planner_model == "planner-explicit"
    assert args.implementer_timeout == 1811
    assert args.git_message_timeout == 311
    assert args.gh_timeout == 19
    assert args.gh_global_rate == 4.5
    assert args.gh_global_burst == 9
    assert args.rate_guard_threshold == 321
    assert args.log_format == "json"
    assert args.pi_isolation_adapter == "operator-broker"
    assert args.pi_dir == Path("/tmp/explicit-pi")
