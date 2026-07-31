"""Static guards for provider-neutral direct-agent automation dispatch."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOTS = (REPO_ROOT / "hephaestus", REPO_ROOT / "scripts")

NEUTRAL_RUNTIME_CALL_NAMES = {
    "resolve_agent",
    "direct_agent_model",
    "run_agent_text",
    "run_agent_session",
    "resume_agent_session",
    "session_agent_matches",
    "uses_direct_agent_runner",
}

DIRECT_PROVIDER_ONLY_NAMES = {
    "is_codex",
    "is_pi",
    "run_codex_text",
    "run_codex_session",
    "resume_codex_session",
    "codex_json_stdout",
    "run_pi_text",
    "run_pi_session",
    "run_pi_smoke_session",
    "resume_pi_session",
    "_invoke_pi_session",
    "_run_pi_command",
}

SUBPROCESS_EXECUTION_NAMES = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
}
OS_EXECUTION_NAMES = {
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
    "execvpe",
}

DIRECT_PROVIDER_ADAPTER_EXCEPTIONS = {
    "scripts/pi_smoke.py": {"run_pi_smoke_session"},
}


def _node_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_pi_command_literal(node: ast.AST) -> bool:
    """Return whether a literal command expression invokes the Pi binary."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.strip()
        return value == "pi" or value.startswith("pi ") or value.endswith("/pi")
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _is_pi_command_literal(node.elts[0])
    return False


def _assignment_target_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    """Return simple names bound by a command assignment."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _pi_command_names(tree: ast.AST) -> set[str]:
    """Discover simple variables assigned a literal Pi command."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and node.value is not None
            and _is_pi_command_literal(node.value)
        ):
            names.update(_assignment_target_names(node))
    return names


def _execution_aliases(tree: ast.AST, module: str, names: set[str]) -> set[str]:
    """Return direct-import aliases for vetted process-execution functions."""
    aliases = set(names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        aliases.update(alias.asname or alias.name for alias in node.names if alias.name in names)
    return aliases


def _call_uses_pi_command(node: ast.Call, command_names: set[str]) -> bool:
    """Return whether a process-execution call receives a known Pi command."""
    return any(
        _is_pi_command_literal(argument)
        or (isinstance(argument, ast.Name) and argument.id in command_names)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
    )


def _provider_neutral_runtime_files() -> list[str]:
    """Discover every non-adapter source file that invokes the neutral runtime.

    The guarded set is derived from the actual entry points rather than a
    hand-maintained inventory, so a new automation session or resume caller
    cannot silently escape the provider-neutral dispatch contract.
    """
    runtime_path = REPO_ROOT / "hephaestus" / "agents" / "runtime.py"
    paths: list[str] = []
    for root in SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path == runtime_path:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                isinstance(node, ast.Call) and _node_name(node.func) in NEUTRAL_RUNTIME_CALL_NAMES
                for node in ast.walk(tree)
            ):
                paths.append(path.relative_to(REPO_ROOT).as_posix())
    return paths


def _provider_adapter_violations(
    tree: ast.AST,
    *,
    allowed_names: set[str] | None = None,
) -> list[str]:
    """Return imports or calls that bypass the shared runtime adapter."""
    allowed = allowed_names or set()
    violations: list[str] = []
    pi_command_names = _pi_command_names(tree)
    subprocess_execution_names = _execution_aliases(
        tree,
        "subprocess",
        SUBPROCESS_EXECUTION_NAMES,
    )
    os_execution_names = _execution_aliases(tree, "os", OS_EXECUTION_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            offenders = (imported & DIRECT_PROVIDER_ONLY_NAMES) - allowed
            if offenders:
                violations.append(f"line {node.lineno}: imports {sorted(offenders)}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            name = _node_name(node.value)
            if name in DIRECT_PROVIDER_ONLY_NAMES and name not in allowed:
                violations.append(f"line {node.lineno}: aliases {name}")
        elif isinstance(node, ast.Call):
            name = _node_name(node.func)
            if name in DIRECT_PROVIDER_ONLY_NAMES and name not in allowed:
                violations.append(f"line {node.lineno}: calls {name}()")
            elif (
                name == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in DIRECT_PROVIDER_ONLY_NAMES
                and node.args[1].value not in allowed
            ):
                violations.append(f"line {node.lineno}: looks up {node.args[1].value}()")
            elif name in subprocess_execution_names and _call_uses_pi_command(
                node, pi_command_names
            ):
                violations.append(f"line {node.lineno}: runs pi subprocess")
            elif name in os_execution_names and _call_uses_pi_command(node, pi_command_names):
                violations.append(f"line {node.lineno}: runs pi OS execution")
    return violations


def _direct_provider_string_compare(node: ast.Compare) -> bool:
    return any(
        isinstance(item, ast.Constant) and item.value in {"codex", "pi"} for item in ast.walk(node)
    )


def test_direct_provider_guard_rejects_aliased_adapter_imports() -> None:
    """Provider adapters cannot evade the import guard through an alias."""
    tree = ast.parse("from hephaestus.agents.runtime import run_pi_session as invoke_pi_session\n")

    assert _provider_adapter_violations(tree) == ["line 1: imports ['run_pi_session']"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import hephaestus.agents.runtime as runtime\n"
            "invoke = runtime.run_pi_session\n"
            "invoke('prompt')\n",
            "line 2: aliases run_pi_session",
        ),
        (
            "import hephaestus.agents.runtime as runtime\n"
            "getattr(runtime, 'run_pi_session')('prompt')\n",
            "line 2: looks up run_pi_session()",
        ),
        (
            "import subprocess\nsubprocess.run(['pi', '--mode', 'json'])\n",
            "line 2: runs pi subprocess",
        ),
        (
            "from hephaestus.agents.runtime import _invoke_pi_session as invoke\n",
            "line 1: imports ['_invoke_pi_session']",
        ),
        (
            "from subprocess import run as execute\n"
            "command = ['pi', '--mode', 'json']\n"
            "execute(command)\n",
            "line 3: runs pi subprocess",
        ),
        (
            "import os\ncommand = 'pi --mode json'\nos.system(command)\n",
            "line 3: runs pi OS execution",
        ),
        (
            "import hephaestus.agents.runtime as runtime\n"
            "getattr(runtime, '_run_pi_command')('prompt')\n",
            "line 2: looks up _run_pi_command()",
        ),
    ],
)
def test_direct_provider_guard_rejects_indirect_pi_execution(
    source: str,
    expected: str,
) -> None:
    """Provider adapters cannot evade the guard through aliases or raw execution."""
    tree = ast.parse(source)

    assert _provider_adapter_violations(tree) == [expected]


def test_direct_provider_guard_detects_membership_style_provider_branches() -> None:
    """Provider-specific membership checks are as risky as equality branches."""
    tree = ast.parse("if agent in {'pi', 'codex'}:\n    pass\n")
    comparison = tree.body[0]

    assert isinstance(comparison, ast.If)
    assert isinstance(comparison.test, ast.Compare)
    assert _direct_provider_string_compare(comparison.test)


@pytest.mark.parametrize("relative_path", _provider_neutral_runtime_files())
def test_direct_agent_dispatch_has_no_provider_specific_runtime_branches(
    relative_path: str,
) -> None:
    """Automation call sites must route Codex and Pi through neutral runtime helpers."""
    path = REPO_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _provider_adapter_violations(tree)
    violations.extend(
        f"line {node.lineno}: compares against a direct provider"
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and _direct_provider_string_compare(node)
    )

    assert violations == []


def test_direct_agent_dispatch_guard_discovers_all_current_runtime_callers() -> None:
    """The automatic inventory covers text, session, and resume call sites."""
    relative_paths = _provider_neutral_runtime_files()

    assert relative_paths
    assert "hephaestus/automation/ci_fix_flow.py" in relative_paths
    assert "hephaestus/automation/pipeline/worker_pool.py" in relative_paths


def test_direct_provider_adapters_are_confined_to_runtime() -> None:
    """Only the explicit, tool-free smoke seam may bypass the neutral boundary."""
    runtime_path = REPO_ROOT / "hephaestus" / "agents" / "runtime.py"
    violations: list[str] = []
    for root in (REPO_ROOT / "hephaestus", REPO_ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            if path == runtime_path:
                continue
            relative_path = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(
                f"{relative_path} {violation}"
                for violation in _provider_adapter_violations(
                    tree,
                    allowed_names=DIRECT_PROVIDER_ADAPTER_EXCEPTIONS.get(relative_path),
                )
            )

    assert violations == []
