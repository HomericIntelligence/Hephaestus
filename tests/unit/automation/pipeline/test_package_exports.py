"""Tests for the pipeline package's static and runtime export surfaces."""

import ast
from pathlib import Path

_PACKAGE_INIT = Path(__file__).parents[4] / "hephaestus" / "automation" / "pipeline" / "__init__.py"


def test_lazy_coordinator_exports_are_available_to_type_checkers() -> None:
    """Import each lazy coordinator export from its owner for type checks."""
    tree = ast.parse(_PACKAGE_INIT.read_text(encoding="utf-8"))
    type_checking_body = next(
        node.body
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )

    imports = {
        (node.module, alias.name)
        for node in type_checking_body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert imports >= {("coordinator_types", "PipelineConfig"), ("coordinator", "run_pipeline")}
    assert ("coordinator", "PipelineConfig") not in imports
