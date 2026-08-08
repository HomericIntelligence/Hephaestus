"""Executable runtime import-graph guard for the automation product layer."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

_AUTOMATION_ROOT = Path(__file__).resolve().parents[3] / "hephaestus" / "automation"
_AUTOMATION_PREFIX = "hephaestus.automation"
_COMPONENT_PACKAGES = frozenset({"hephaestus.automation.github_api"})


def _component(module: str) -> str:
    """Normalize the documented compatibility façade into one graph node."""
    for package in _COMPONENT_PACKAGES:
        if module == package or module.startswith(f"{package}."):
            return package
    return module


def _type_checking_value(test: ast.expr) -> bool | None:
    """Return the static value of a common ``TYPE_CHECKING`` guard."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id in {"typing", "_typing"}
    ):
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        value = _type_checking_value(test.operand)
        return None if value is None else not value
    return None


def _visit_runtime_imports(node: ast.AST) -> Iterator[ast.Import | ast.ImportFrom]:
    """Yield imports that execute at runtime, including function-local imports."""
    if isinstance(node, ast.If):
        guard = _type_checking_value(node.test)
        if guard is True:
            for statement in node.orelse:
                yield from _visit_runtime_imports(statement)
            return
        if guard is False:
            for statement in node.body:
                yield from _visit_runtime_imports(statement)
            return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node
    for descendant in ast.iter_child_nodes(node):
        yield from _visit_runtime_imports(descendant)


def _module_names(root: Path) -> set[str]:
    """Return automation module names represented by Python files below *root*."""
    modules: set[str] = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join((_AUTOMATION_PREFIX, *parts))
        modules.add(name)
    return modules


def _relative_module(
    current: str,
    level: int,
    imported: str | None,
    *,
    current_is_package: bool,
) -> str:
    """Resolve an ``ImportFrom`` target using Python's package-relative rules."""
    current_parts = current.split(".")
    package_parts = current_parts if current_is_package else current_parts[:-1]
    base_length = len(package_parts) - (level - 1)
    base = package_parts[: max(base_length, 0)]
    if imported:
        base.append(imported)
    return ".".join(base)


def _lazy_export_targets(tree: ast.Module) -> Iterator[str]:
    """Yield string module targets from assigned lazy-export dictionaries."""
    for statement in tree.body:
        value: ast.expr | None = None
        if (
            isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_LAZY_EXPORTS"
                for target in statement.targets
            )
        ) or (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "_LAZY_EXPORTS"
        ):
            value = statement.value
        if not isinstance(value, ast.Dict):
            continue
        for item in value.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                yield item.value


def _module_path(root: Path, module: str) -> Path:
    """Resolve an automation module name to its Python source path."""
    relative_name = module.removeprefix(f"{_AUTOMATION_PREFIX}.")
    if relative_name == module:
        return root / "__init__.py"
    path = root.joinpath(*relative_name.split("."))
    return path / "__init__.py" if path.is_dir() else path.with_suffix(".py")


def _runtime_import_targets(
    module: str,
    path: Path,
    tree: ast.Module,
    modules: set[str],
) -> Iterator[str]:
    """Yield automation targets for runtime imports in one source module."""
    for node in _visit_runtime_imports(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
            continue

        imported_module = (
            _relative_module(
                module,
                node.level,
                node.module,
                current_is_package=path.name == "__init__.py",
            )
            if node.level
            else (node.module or "")
        )
        yield imported_module
        for alias in node.names:
            child = f"{imported_module}.{alias.name}" if imported_module else alias.name
            if child in modules:
                yield child


def _build_import_graph(root: Path) -> dict[str, set[str]]:
    """Build the normalized runtime module graph rooted at *root*."""
    modules = _module_names(root)
    graph: dict[str, set[str]] = {}

    def add_edge(source: str, target: str) -> None:
        if not target.startswith(f"{_AUTOMATION_PREFIX}.") and target != _AUTOMATION_PREFIX:
            return
        normalized_source = _component(source)
        normalized_target = _component(target)
        graph.setdefault(normalized_source, set())
        graph.setdefault(normalized_target, set())
        if normalized_source == normalized_target and source != target:
            return
        graph[normalized_source].add(normalized_target)

    for module in modules:
        normalized_module = _component(module)
        graph.setdefault(normalized_module, set())
        path = _module_path(root, module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target in _runtime_import_targets(module, path, tree, modules):
            add_edge(module, target)
        for target in _lazy_export_targets(tree):
            add_edge(module, target)

    return graph


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    """Return deterministic cyclic strongly connected components."""
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(graph.get(node, ())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == node:
                break
        component_tuple = tuple(sorted(component))
        if len(component_tuple) > 1 or node in graph.get(node, set()):
            components.append(component_tuple)

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components)


def _write_synthetic_graph(tmp_path: Path, fixture_name: str) -> Path:
    """Write a small cycle fixture under the same package prefix as production."""
    root = tmp_path / "hephaestus" / "automation"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")

    fixtures = {
        "function_local_cycle": {
            "a.py": "def load():\n    import hephaestus.automation.b\n",
            "b.py": "def load():\n    import hephaestus.automation.a\n",
        },
        "child_to_ancestor_cycle": {
            "pkg/__init__.py": "from . import child\n",
            "pkg/child.py": "from .. import pkg\n",
        },
        "from_package_import_submodule_cycle": {
            "a.py": "from . import b\n",
            "b.py": "from . import a\n",
        },
        "lazy_export_cycle": {
            "a.py": '_LAZY_EXPORTS = {"b": "hephaestus.automation.b"}\n',
            "b.py": "from . import a\n",
        },
        "annotated_lazy_export_cycle": {
            "a.py": '_LAZY_EXPORTS: dict[str, str] = {"b": "hephaestus.automation.b"}\n',
            "b.py": "from . import a\n",
        },
    }
    for relative, content in fixtures[fixture_name].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_automation_runtime_import_graph_is_acyclic() -> None:
    """The runtime automation graph must contain no strongly connected component."""
    graph = _build_import_graph(_AUTOMATION_ROOT)
    assert _strongly_connected_components(graph) == []


def test_strongly_connected_components_reports_self_loops() -> None:
    """A module importing itself is a cyclic component too."""
    assert _strongly_connected_components({"module": {"module"}}) == [("module",)]


@pytest.mark.parametrize(
    "fixture_name",
    [
        "function_local_cycle",
        "child_to_ancestor_cycle",
        "from_package_import_submodule_cycle",
        "lazy_export_cycle",
        "annotated_lazy_export_cycle",
    ],
)
def test_graph_detects_synthetic_cycles(tmp_path: Path, fixture_name: str) -> None:
    """Every supported import form participates in cycle detection."""
    root = _write_synthetic_graph(tmp_path, fixture_name)
    assert _strongly_connected_components(_build_import_graph(root))
