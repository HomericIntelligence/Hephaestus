"""Guard: no source-line-number assertions in tests (issue #2122, root cause of #2056).

Line numbers are volatile — any edit above a function shifts them, turning
routine drift into test failures with no behavior change. Under concurrent
merging the "correct" line is only knowable at merge instant, so a test that
pins ``inspect.getsourcelines(fn)[1]`` (or ``fn.__code__.co_firstlineno``) to a
literal strands every open PR carrying the change. Assert symbol *presence* /
that the function *resolves* instead.

This is an executable invariant (alongside ``test_import_surface.py``,
``test_automation_boundary.py``, ``test_omit_allowlist.py``): an AST walk over
``tests/`` that flags the volatile-line-number patterns, plus synthetic-source
self-tests so the guard can never pass vacuously.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[2]  # tests/

# inspect functions whose return value's ``[1]`` element is a source line number.
_BANNED_LINE_FUNCS = frozenset({"getsourcelines", "findsource"})
# Sanctioned files (none today); mirror ``test_zero_io_imports._ALLOWLIST``.
_ALLOWLIST: frozenset[str] = frozenset()


def _is_banned_line_subscript(node: ast.Subscript) -> bool:
    """Return True for ``getsourcelines(...)[1]`` / ``findsource(...)[1]``.

    The ``[1]`` element of both return values is the starting line number;
    ``[0]`` (the source text) is fine and must not trip.
    """
    if not (isinstance(node.slice, ast.Constant) and node.slice.value == 1):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else (func.id if isinstance(func, ast.Name) else "")
    )
    return name in _BANNED_LINE_FUNCS


def _collect_violations(tree: ast.AST, filename: str) -> list[str]:
    """Return volatile-line-number violations found anywhere in *tree*."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "co_firstlineno":
            violations.append(f"{filename}:{node.lineno}: .co_firstlineno access")
        elif isinstance(node, ast.Subscript) and _is_banned_line_subscript(node):
            violations.append(f"{filename}:{node.lineno}: getsourcelines/findsource(...)[1]")
    return violations


def test_no_source_line_number_assertions_in_tests() -> None:
    """No test may compare a source line number to a literal / doc-derived value."""
    violations: list[str] = []
    for py in sorted(_TESTS_DIR.rglob("*.py")):
        if py.name in _ALLOWLIST:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        violations.extend(_collect_violations(tree, str(py.relative_to(_TESTS_DIR.parent))))
    assert not violations, (
        "Source line numbers are volatile (issue #2122); assert symbol presence "
        "instead of line numbers:\n" + "\n".join(violations)
    )


def test_guard_detects_synthetic_violations() -> None:
    """Negative test: a broken collector must not pass vacuously.

    Banned patterns living inside these string literals do not produce AST
    ``Subscript``/``Attribute`` nodes, so this file itself never trips the
    guard — no self-exemption is needed.
    """
    synthetic = (
        "import inspect\n"
        "assert inspect.getsourcelines(fn)[1] == 42\n"
        "lineno = inspect.findsource(fn)[1]\n"  # indirection still trips
        "assert fn.__code__.co_firstlineno == expected\n"
        "src = inspect.getsourcelines(fn)[0]\n"  # [0] (source text) is fine
    )
    violations = _collect_violations(ast.parse(synthetic), "<synthetic>")
    assert len(violations) == 3, violations
    assert any(":2:" in v for v in violations)
    assert any(":3:" in v for v in violations)
    assert any(":4:" in v for v in violations)
    assert not any(":5:" in v for v in violations)
