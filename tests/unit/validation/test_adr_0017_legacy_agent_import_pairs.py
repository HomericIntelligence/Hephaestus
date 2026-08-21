"""Guard ADR-0017's frozen legacy consumer/module exceptions."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUTOMATION_ROOT = _REPO_ROOT / "hephaestus" / "automation"
_LEGACY_MODULES = frozenset({"claude_invoke", "claude_models", "claude_timeouts"})

_APPROVED_DIRECT_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("_implement_phase.py", "claude_invoke"),
        ("_implement_phase.py", "claude_models"),
        ("advise_runner.py", "claude_invoke"),
        ("audit_reviewer.py", "claude_invoke"),
        ("comment_difficulty.py", "claude_invoke"),
        ("comment_difficulty.py", "claude_models"),
        ("learn.py", "claude_models"),
        ("pipeline/worker_pool.py", "claude_invoke"),
        ("plan_reviewer.py", "claude_invoke"),
        ("plan_reviewer.py", "claude_models"),
        ("post_merge_processor.py", "claude_invoke"),
        ("post_merge_processor.py", "claude_models"),
        ("pr_manager.py", "claude_invoke"),
        ("pr_manager.py", "claude_models"),
        ("pr_review_core.py", "claude_invoke"),
        ("pr_review_core.py", "claude_models"),
    }
)


def _resolve_from_module(relative: Path, node: ast.ImportFrom) -> str:
    """Resolve an import-from module against its importing automation package."""
    if not node.level:
        return node.module or ""

    package = ".".join(("hephaestus", "automation", *relative.parent.parts))
    relative_name = f"{'.' * node.level}{node.module or ''}"
    try:
        return resolve_name(relative_name, package)
    except ImportError:
        return ""


def _legacy_targets(relative: Path, node: ast.Import | ast.ImportFrom) -> set[str]:
    """Return legacy automation modules imported by one AST node."""
    if isinstance(node, ast.Import):
        return {
            target
            for alias in node.names
            for target in _LEGACY_MODULES
            if alias.name in {target, f"hephaestus.automation.{target}"}
        }

    module = _resolve_from_module(relative, node)
    if module in _LEGACY_MODULES:
        return {module}
    if module.startswith("hephaestus.automation."):
        target = module.removeprefix("hephaestus.automation.").split(".", 1)[0]
        return {target} if target in _LEGACY_MODULES else set()
    if module == "hephaestus.automation":
        return {alias.name for alias in node.names if alias.name in _LEGACY_MODULES}
    return set()


def test_relative_imports_resolve_legacy_modules() -> None:
    """Relative spellings of legacy modules must be subject to the guard."""
    cases = (
        (Path("example.py"), "from . import claude_invoke", {"claude_invoke"}),
        (Path("example.py"), "from .claude_models import reviewer_model", {"claude_models"}),
        (
            Path("pipeline/example.py"),
            "from ...automation import claude_invoke",
            {"claude_invoke"},
        ),
        (
            Path("pipeline/stages/example.py"),
            "from ....automation import claude_models",
            {"claude_models"},
        ),
    )

    for relative, statement, expected in cases:
        node = ast.parse(statement).body[0]
        assert isinstance(node, ast.ImportFrom)
        assert _legacy_targets(relative, node) == expected


def test_absolute_import_resolves_legacy_module() -> None:
    """Absolute imports of a legacy module remain subject to the guard."""
    node = ast.parse("from hephaestus.automation import claude_invoke").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert _legacy_targets(Path("example.py"), node) == {"claude_invoke"}


def test_import_statement_resolves_legacy_module() -> None:
    """Plain import statements of a legacy module remain subject to the guard."""
    node = ast.parse("import hephaestus.automation.claude_models").body[0]
    assert isinstance(node, ast.Import)
    assert _legacy_targets(Path("example.py"), node) == {"claude_models"}


def _legacy_import_pairs(relative: Path, tree: ast.AST) -> set[tuple[str, str]]:
    """Collect direct legacy consumer/module pairs from one parsed source tree."""
    return {
        (relative.as_posix(), target)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for target in _legacy_targets(relative, node)
    }


def test_multiple_import_nodes_preserve_one_consumer_module_pair() -> None:
    """The policy deliberately ignores symbols and import-node counts."""
    tree = ast.parse(
        "from .claude_invoke import invoke_claude_with_session\n"
        "from .claude_invoke import ClaudeSession\n"
    )
    assert _legacy_import_pairs(Path("example.py"), tree) == {("example.py", "claude_invoke")}


def _collect_direct_imports() -> set[tuple[str, str]]:
    """Collect legacy consumer/module pairs beneath the automation product layer."""
    imports: set[tuple[str, str]] = set()
    for source in sorted(_AUTOMATION_ROOT.rglob("*.py")):
        relative_path = source.relative_to(_AUTOMATION_ROOT)
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imports.update(_legacy_import_pairs(relative_path, tree))
    return imports


def test_direct_imports_match_frozen_migration_baseline() -> None:
    """Source imports must exactly match ADR-0017's frozen migration debt."""
    actual = _collect_direct_imports()
    assert actual == _APPROVED_DIRECT_IMPORTS, (
        "ADR-0017 consumer/module pair baseline drifted: "
        f"added={sorted(actual - _APPROVED_DIRECT_IMPORTS)}, "
        f"removed={sorted(_APPROVED_DIRECT_IMPORTS - actual)}"
    )


def test_approved_importers_stay_in_automation_product_layer() -> None:
    """Legacy exceptions must never name library-layer consumers."""
    root = _AUTOMATION_ROOT.resolve()
    invalid = []
    for relative, target in sorted(_APPROVED_DIRECT_IMPORTS):
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            invalid.append((relative, target))
    assert not invalid, f"non-automation or missing approved importers: {invalid}"
