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
    "agent_supports_model_reasoning_effort",
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
    "_pi_base_cmd",
    "_pi_smoke_base_cmd",
    "_pi_sandbox_args",
    "_run_pi_command",
}
DIRECT_RUNNER_BINARIES = ("pi", "codex")

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


def _direct_runner_command_literal(node: ast.AST) -> str | None:
    """Return the direct-runner binary invoked by a literal command expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.strip()
        for binary in DIRECT_RUNNER_BINARIES:
            if value == binary or value.startswith(f"{binary} ") or value.endswith(f"/{binary}"):
                return binary
        return None
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _direct_runner_command_literal(node.elts[0])
    return None


def _assignment_target_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    """Return simple names bound by a command assignment."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _direct_runner_command_names(tree: ast.AST) -> dict[str, str]:
    """Discover simple variables assigned a literal direct-runner command."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            binary = _direct_runner_command_literal(node.value)
            if binary:
                names.update(dict.fromkeys(_assignment_target_names(node), binary))
    return names


def _module_import_aliases(tree: ast.AST, module: str) -> set[str]:
    """Return names that refer directly to one guarded standard-library module."""
    aliases = {module}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        aliases.update(alias.asname or alias.name for alias in node.names if alias.name == module)
    return aliases


def _execution_reference_name(
    node: ast.AST,
    module_aliases: set[str],
    names: set[str],
    aliases: set[str],
    mappings: dict[tuple[str, str], str] | None = None,
) -> str:
    """Resolve a guarded executor through direct, assigned, mapped, or reflected references."""
    if isinstance(node, ast.Name):
        return node.id if node.id in aliases else ""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in module_aliases
        and node.attr in names
    ):
        return node.attr
    if isinstance(node, ast.Call) and _node_name(node.func) == "getattr" and len(node.args) >= 2:
        name = _mapping_key(node.args[1])
        if (
            isinstance(node.args[0], ast.Name)
            and node.args[0].id in module_aliases
            and name in names
        ):
            return name
    if not isinstance(node, ast.Subscript):
        return ""
    name = _mapping_key(node.slice)
    if isinstance(node.value, ast.Name) and name is not None and mappings is not None:
        mapped_name = mappings.get((node.value.id, name), "")
        if mapped_name:
            return mapped_name
    if name not in names:
        return ""
    if (
        isinstance(node.value, ast.Attribute)
        and node.value.attr == "__dict__"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id in module_aliases
    ):
        return name
    if (
        isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "vars"
        and node.value.args
        and isinstance(node.value.args[0], ast.Name)
        and node.value.args[0].id in module_aliases
    ):
        return name
    return ""


def _copy_execution_mapping_aliases(
    targets: set[str],
    value: ast.AST,
    mappings: dict[tuple[str, str], str],
) -> bool:
    """Copy literal executor-map bindings when a map is assigned to an alias."""
    if not isinstance(value, ast.Name):
        return False
    changed = False
    for (mapping_name, mapping_key), mapping_value in tuple(mappings.items()):
        if mapping_name != value.id:
            continue
        for target in targets:
            alias_key = (target, mapping_key)
            if mappings.get(alias_key) != mapping_value:
                mappings[alias_key] = mapping_value
                changed = True
    return changed


def _update_literal_execution_mapping_entries(
    targets: set[str],
    value: ast.AST,
    module_aliases: set[str],
    names: set[str],
    aliases: set[str],
    mappings: dict[tuple[str, str], str],
) -> bool:
    """Bind literal dictionary entries to guarded process-execution functions."""
    if not isinstance(value, ast.Dict):
        return False
    changed = False
    for key_node, value_node in zip(value.keys, value.values, strict=True):
        if key_node is None:
            continue
        literal_key = _mapping_key(key_node)
        name = _execution_reference_name(value_node, module_aliases, names, aliases, mappings)
        if literal_key is None or not name:
            continue
        for target in targets:
            mapping_key = (target, literal_key)
            if mappings.get(mapping_key) != name:
                mappings[mapping_key] = name
                changed = True
    return changed


def _execution_bindings(
    tree: ast.AST,
    module: str,
    names: set[str],
) -> tuple[set[str], dict[tuple[str, str], str]]:
    """Return aliases and literal mapping entries for guarded executors."""
    aliases = set(names)
    mappings: dict[tuple[str, str], str] = {}
    module_aliases = _module_import_aliases(tree, module)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != module:
            continue
        aliases.update(alias.asname or alias.name for alias in node.names if alias.name in names)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            assert assignment.value is not None
            name = _execution_reference_name(
                assignment.value,
                module_aliases,
                names,
                aliases,
                mappings,
            )
            targets = _assignment_target_names(assignment)
            if name:
                for target in targets:
                    if target not in aliases:
                        aliases.add(target)
                        changed = True
            changed |= _copy_execution_mapping_aliases(targets, assignment.value, mappings)
            changed |= _update_literal_execution_mapping_entries(
                targets,
                assignment.value,
                module_aliases,
                names,
                aliases,
                mappings,
            )
    return aliases, mappings


def _call_direct_runner_command(
    node: ast.Call,
    command_names: dict[str, str],
) -> str | None:
    """Return the direct-runner binary passed to a process-execution call."""
    for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
        binary = _direct_runner_command_literal(argument)
        if binary:
            return binary
        if isinstance(argument, ast.Name) and argument.id in command_names:
            return command_names[argument.id]
    return None


def _mapping_key(node: ast.AST) -> str | None:
    """Return a string mapping key when the AST node is a literal string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _neutral_runtime_name(
    node: ast.AST,
    aliases: dict[str, str],
    mappings: dict[tuple[str, str], str],
) -> str:
    """Resolve a neutral runtime helper through a direct, alias, or mapping reference."""
    name = _node_name(node)
    if name in NEUTRAL_RUNTIME_CALL_NAMES:
        return name
    if isinstance(node, ast.Call) and _node_name(node.func) == "getattr" and len(node.args) >= 2:
        getattr_name = _mapping_key(node.args[1])
        if getattr_name in NEUTRAL_RUNTIME_CALL_NAMES:
            return getattr_name
    if isinstance(node, ast.Name):
        return aliases.get(node.id, "")
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        key = _mapping_key(node.slice)
        if key is not None:
            return mappings.get((node.value.id, key), "")
    return ""


def _neutral_runtime_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Return imports that bind a neutral runtime helper to a local name."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "hephaestus.agents.runtime":
            continue
        for imported in node.names:
            if imported.name in NEUTRAL_RUNTIME_CALL_NAMES:
                aliases[imported.asname or imported.name] = imported.name
    return aliases


def _update_neutral_aliases(
    targets: set[str],
    value: ast.AST,
    aliases: dict[str, str],
    mappings: dict[tuple[str, str], str],
) -> bool:
    """Bind simple aliases to their resolved neutral runtime helper."""
    name = _neutral_runtime_name(value, aliases, mappings)
    if not name:
        return False
    changed = False
    for target in targets:
        if aliases.get(target) != name:
            aliases[target] = name
            changed = True
    return changed


def _copy_neutral_mapping_aliases(
    targets: set[str],
    value: ast.AST,
    mappings: dict[tuple[str, str], str],
) -> bool:
    """Copy literal mapping bindings when a mapping name is assigned to an alias."""
    if not isinstance(value, ast.Name):
        return False
    changed = False
    for (mapping_name, mapping_key), mapping_value in tuple(mappings.items()):
        if mapping_name != value.id:
            continue
        for target in targets:
            alias_key = (target, mapping_key)
            if mappings.get(alias_key) != mapping_value:
                mappings[alias_key] = mapping_value
                changed = True
    return changed


def _update_literal_neutral_mapping_entries(
    targets: set[str],
    value: ast.AST,
    aliases: dict[str, str],
    mappings: dict[tuple[str, str], str],
) -> bool:
    """Bind literal dictionary entries to neutral runtime helpers."""
    if not isinstance(value, ast.Dict):
        return False
    changed = False
    for key_node, value_node in zip(value.keys, value.values, strict=True):
        if key_node is None:
            continue
        literal_key = _mapping_key(key_node)
        name = _neutral_runtime_name(value_node, aliases, mappings)
        if literal_key is None or not name:
            continue
        for target in targets:
            mapping_key = (target, literal_key)
            if mappings.get(mapping_key) != name:
                mappings[mapping_key] = name
                changed = True
    return changed


def _update_neutral_runtime_bindings(
    assignments: list[ast.Assign | ast.AnnAssign],
    aliases: dict[str, str],
    mappings: dict[tuple[str, str], str],
) -> bool:
    """Resolve one fixed-point pass of neutral runtime aliases and mapping entries."""
    changed = False
    for assignment in assignments:
        value = assignment.value
        assert value is not None
        targets = _assignment_target_names(assignment)
        changed |= _update_neutral_aliases(targets, value, aliases, mappings)
        changed |= _copy_neutral_mapping_aliases(targets, value, mappings)
        changed |= _update_literal_neutral_mapping_entries(targets, value, aliases, mappings)
    return changed


def _neutral_runtime_bindings(tree: ast.AST) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Return aliases and literal mapping entries bound to neutral runtime helpers."""
    aliases = _neutral_runtime_import_aliases(tree)
    mappings: dict[tuple[str, str], str] = {}
    assignments: list[ast.Assign | ast.AnnAssign] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]
    while _update_neutral_runtime_bindings(assignments, aliases, mappings):
        pass
    return aliases, mappings


def _uses_neutral_runtime_call(tree: ast.AST) -> bool:
    """Return whether code invokes a neutral runtime helper through a safe binding."""
    aliases, mappings = _neutral_runtime_bindings(tree)
    return any(
        isinstance(node, ast.Call) and _neutral_runtime_name(node.func, aliases, mappings)
        for node in ast.walk(tree)
    )


def _reflective_provider_adapter_name(node: ast.AST) -> str:
    """Return a provider adapter named by a literal reflective lookup."""
    if isinstance(node, ast.Subscript):
        key = _mapping_key(node.slice)
        return key if key in DIRECT_PROVIDER_ONLY_NAMES else ""
    if not isinstance(node, ast.Call):
        return ""
    lookup_name = _node_name(node.func)
    key_index = 1 if lookup_name == "getattr" else 0 if lookup_name == "get" else None
    if key_index is None or len(node.args) <= key_index:
        return ""
    key = _mapping_key(node.args[key_index])
    return key if key in DIRECT_PROVIDER_ONLY_NAMES else ""


def _direct_provider_adapter_name(node: ast.AST) -> str:
    """Return a direct provider adapter referenced anywhere in an expression."""
    for item in ast.walk(node):
        reflective_name = _reflective_provider_adapter_name(item)
        if reflective_name:
            return reflective_name
        name = _node_name(item)
        if name in DIRECT_PROVIDER_ONLY_NAMES:
            return name
    return ""


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
            if _uses_neutral_runtime_call(tree):
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
    direct_runner_command_names = _direct_runner_command_names(tree)
    subprocess_execution_names, subprocess_execution_mappings = _execution_bindings(
        tree,
        "subprocess",
        SUBPROCESS_EXECUTION_NAMES,
    )
    subprocess_module_aliases = _module_import_aliases(tree, "subprocess")
    os_execution_names, os_execution_mappings = _execution_bindings(
        tree,
        "os",
        OS_EXECUTION_NAMES,
    )
    os_module_aliases = _module_import_aliases(tree, "os")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            offenders = (imported & DIRECT_PROVIDER_ONLY_NAMES) - allowed
            if offenders:
                violations.append(f"line {node.lineno}: imports {sorted(offenders)}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            name = _direct_provider_adapter_name(node.value)
            if name in DIRECT_PROVIDER_ONLY_NAMES and name not in allowed:
                violations.append(f"line {node.lineno}: aliases {name}")
        elif isinstance(node, ast.Call):
            name = _node_name(node.func)
            if name in DIRECT_PROVIDER_ONLY_NAMES and name not in allowed:
                violations.append(f"line {node.lineno}: calls {name}()")
            else:
                reflective_name = _reflective_provider_adapter_name(node.func)
                if reflective_name and reflective_name not in allowed:
                    violations.append(f"line {node.lineno}: looks up {reflective_name}()")
                    continue
                binary = _call_direct_runner_command(node, direct_runner_command_names)
                subprocess_execution_name = _execution_reference_name(
                    node.func,
                    subprocess_module_aliases,
                    SUBPROCESS_EXECUTION_NAMES,
                    subprocess_execution_names,
                    subprocess_execution_mappings,
                )
                os_execution_name = _execution_reference_name(
                    node.func,
                    os_module_aliases,
                    OS_EXECUTION_NAMES,
                    os_execution_names,
                    os_execution_mappings,
                )
                if subprocess_execution_name and binary:
                    violations.append(f"line {node.lineno}: runs {binary} subprocess")
                elif os_execution_name and binary:
                    violations.append(f"line {node.lineno}: runs {binary} OS execution")
    return violations


def _direct_provider_string_compare(node: ast.Compare) -> bool:
    return any(
        isinstance(item, ast.Constant) and item.value in {"codex", "pi"} for item in ast.walk(node)
    )


def _provider_specific_branch_violations(tree: ast.AST) -> list[str]:
    """Return direct-provider branches that would fork orchestration."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _direct_provider_string_compare(node):
            violations.append(f"line {node.lineno}: compares against a direct provider")
        elif isinstance(node, ast.Match) and any(
            isinstance(pattern, ast.MatchValue)
            and isinstance(pattern.value, ast.Constant)
            and pattern.value.value in {"codex", "pi"}
            for case in node.cases
            for pattern in ast.walk(case.pattern)
        ):
            violations.append(f"line {node.lineno}: matches against a direct provider")
    return violations


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
            "import subprocess\nsubprocess.run(['codex', 'exec'])\n",
            "line 2: runs codex subprocess",
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
            "import subprocess\nexecute = subprocess.run\nexecute(['pi', '--mode', 'json'])\n",
            "line 3: runs pi subprocess",
        ),
        (
            "import subprocess\ngetattr(subprocess, 'run')(['pi', '--mode', 'json'])\n",
            "line 2: runs pi subprocess",
        ),
        (
            "import subprocess\n"
            "executors = {'run': subprocess.run}\n"
            "executors['run'](['pi', '--mode', 'json'])\n",
            "line 3: runs pi subprocess",
        ),
        (
            "import subprocess\n"
            "executors = {'run': subprocess.run}\n"
            "delegates = executors\n"
            "delegates['run'](['pi', '--mode', 'json'])\n",
            "line 4: runs pi subprocess",
        ),
        (
            "import subprocess\n"
            "executors = {'run': subprocess.run}\n"
            "execute = executors['run']\n"
            "execute(['pi', '--mode', 'json'])\n",
            "line 4: runs pi subprocess",
        ),
        (
            "import os\ncommand = 'pi --mode json'\nos.system(command)\n",
            "line 3: runs pi OS execution",
        ),
        (
            "import os\nexecutors = {'system': os.system}\nexecutors['system']('pi --mode json')\n",
            "line 3: runs pi OS execution",
        ),
        (
            "import os\nexecute = getattr(os, 'system')\nexecute('pi --mode json')\n",
            "line 3: runs pi OS execution",
        ),
        (
            "import os\nos.__dict__['system']('pi --mode json')\n",
            "line 2: runs pi OS execution",
        ),
        (
            "import hephaestus.agents.runtime as runtime\n"
            "getattr(runtime, '_run_pi_command')('prompt')\n",
            "line 2: looks up _run_pi_command()",
        ),
        (
            "import hephaestus.agents.runtime as runtime\n"
            "handlers = {'invoke': runtime._invoke_pi_session}\n"
            "handlers['invoke']('prompt')\n",
            "line 2: aliases _invoke_pi_session",
        ),
        (
            "import hephaestus.agents.runtime as runtime\n"
            "runtime.__dict__['_invoke_pi_session']('prompt')\n",
            "line 2: looks up _invoke_pi_session()",
        ),
        (
            "import hephaestus.agents.runtime as runtime\n"
            "import subprocess\n"
            "subprocess.run(runtime._pi_base_cmd())\n",
            "line 3: calls _pi_base_cmd()",
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


def test_direct_agent_dispatch_guard_discovers_mapping_runtime_callers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mapped neutral calls must be guarded against provider-specific branches."""
    source_root = tmp_path / "hephaestus"
    source_root.mkdir()
    (source_root / "caller.py").write_text(
        "import hephaestus.agents.runtime as runtime\n"
        "handlers = {'run': runtime.run_agent_session}\n"
        "if agent == 'pi':\n"
        "    handlers['run']('prompt')\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "SOURCE_ROOTS", (source_root,))

    assert _provider_neutral_runtime_files() == ["hephaestus/caller.py"]


def test_direct_agent_dispatch_guard_follows_mapping_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mapping aliases must retain neutral runtime bindings for branch checks."""
    source_root = tmp_path / "hephaestus"
    source_root.mkdir()
    source = (
        "import hephaestus.agents.runtime as runtime\n"
        "handlers = {'run': runtime.run_agent_session}\n"
        "dispatch = handlers\n"
        "if agent == 'pi':\n"
        "    dispatch['run']('prompt')\n"
    )
    (source_root / "caller.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "SOURCE_ROOTS", (source_root,))

    assert _provider_neutral_runtime_files() == ["hephaestus/caller.py"]


@pytest.mark.parametrize(
    "source",
    [
        "import hephaestus.agents.runtime as runtime\n"
        "invoke = getattr(runtime, 'run_agent_session')\n"
        "if agent == 'pi':\n"
        "    invoke('prompt')\n",
        "import hephaestus.agents.runtime as runtime\n"
        "if agent == 'pi':\n"
        "    getattr(runtime, 'run_agent_session')('prompt')\n",
    ],
)
def test_direct_agent_dispatch_guard_discovers_getattr_runtime_callers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    """Literal ``getattr`` bindings must not evade the provider branch guard."""
    source_root = tmp_path / "hephaestus"
    source_root.mkdir()
    (source_root / "caller.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "SOURCE_ROOTS", (source_root,))

    assert _provider_neutral_runtime_files() == ["hephaestus/caller.py"]


@pytest.mark.parametrize("relative_path", _provider_neutral_runtime_files())
def test_direct_agent_dispatch_has_no_provider_specific_runtime_branches(
    relative_path: str,
) -> None:
    """Automation call sites must route Codex and Pi through neutral runtime helpers."""
    path = REPO_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = _provider_adapter_violations(tree)
    violations.extend(_provider_specific_branch_violations(tree))

    assert violations == []


def test_direct_agent_dispatch_guard_discovers_all_current_runtime_callers() -> None:
    """The automatic inventory covers text, session, and resume call sites."""
    relative_paths = _provider_neutral_runtime_files()

    assert relative_paths
    assert "hephaestus/automation/pipeline/worker_pool.py" in relative_paths


def test_provider_specific_branch_guard_rejects_standalone_orchestration() -> None:
    """Provider branches must be visible even before a runtime call is added."""
    tree = ast.parse("if agent == 'pi':\n    configure_pi_scope()\n")

    assert _provider_specific_branch_violations(tree) == [
        "line 1: compares against a direct provider"
    ]


def test_provider_specific_branch_guard_rejects_match_orchestration() -> None:
    """Structural pattern matching cannot create a hidden provider fork."""
    tree = ast.parse("match agent:\n    case 'pi':\n        configure_pi_scope()\n")

    assert _provider_specific_branch_violations(tree) == [
        "line 1: matches against a direct provider"
    ]


def test_provider_specific_branches_are_confined_to_runtime_adapter() -> None:
    """Production orchestration cannot fork on direct provider names."""
    runtime_path = REPO_ROOT / "hephaestus" / "agents" / "runtime.py"
    violations: list[str] = []
    for root in SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path == runtime_path:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(
                f"{path.relative_to(REPO_ROOT).as_posix()} {violation}"
                for violation in _provider_specific_branch_violations(tree)
            )

    assert violations == []


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


def test_automation_agent_resolution_binds_models_before_provider_probes() -> None:
    """Every automation resolver supplies the pending model selections."""
    violations: list[str] = []
    automation_root = REPO_ROOT / "hephaestus" / "automation"
    for path in sorted(automation_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _node_name(node.func) != "resolve_agent":
                continue
            if not any(keyword.arg == "model_references" for keyword in node.keywords):
                violations.append(f"{path.relative_to(REPO_ROOT).as_posix()} line {node.lineno}")

    assert violations == []
