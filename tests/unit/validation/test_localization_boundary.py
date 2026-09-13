"""Check the localization boundary for Hephaestus-authored text."""

import ast
from pathlib import Path

_PARSER_CALLS = {
    "ArgumentParser",
    "add_argument",
    "add_argument_group",
    "add_parser",
    "add_subparsers",
}
_DISPLAY_KEYWORDS = {"description", "epilog", "help", "title", "usage"}
_HUMAN_RENDERERS = {
    "format_report",
    "format_stats_table",
    "format_stats_text",
    "format_summary_table",
    "format_system_info",
    "format_text_report",
}
_MACHINE_RENDERERS = {"format_json", "format_json_report", "format_output"}


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_text_boundary(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        if _call_name(node.func) == "text":
            return True
        return bool(
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "template"
            and isinstance(node.func.value, ast.Call)
            and _call_name(node.func.value.func) == "get_localizer"
        )
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.IfExp):
        return _is_text_boundary(node.body) and _is_text_boundary(node.orelse)
    return bool(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "argparse"
        and node.attr == "SUPPRESS"
    )


def _is_argparse_translation_global(node: ast.expr) -> bool:
    return bool(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "argparse"
        and node.attr in {"_", "ngettext"}
    )


def _is_direct_user_message(node: ast.Call) -> bool:
    """Return whether a call contains direct user-facing text."""
    return bool(
        node.args
        and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))
        and not _is_text_boundary(node.args[0])
    )


def _assignment_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return [node.target]
    return []


def _contains_authored_text(node: ast.expr) -> bool:
    """Return whether an expression contains untranslated prose."""
    if _is_text_boundary(node):
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and any(char.isalpha() for char in node.value)
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and any(char.isalpha() for char in value.value)
            for value in node.values
        )
    return any(
        _contains_authored_text(child)
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.expr)
    )


def _walk_scope(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Return nodes in one lexical scope without entering nested functions."""
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(scope.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _human_renderer_violations(path: Path, tree: ast.Module) -> list[str]:
    """Find raw prose that a human-facing renderer assembles."""
    violations: list[str] = []
    renderer_names = set(_HUMAN_RENDERERS)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "print" or not node.args:
            continue
        renderer_names.update(
            name
            for child in ast.walk(node.args[0])
            if isinstance(child, ast.Call)
            and (name := _call_name(child.func)).startswith("format")
            and name not in _MACHINE_RENDERERS
        )
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        if function.name not in renderer_names:
            continue
        for node in ast.walk(function):
            expressions: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Return) and node.value is not None:
                value = node.value
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and node.value is not None
                and isinstance(
                    node.value,
                    (ast.BinOp, ast.Constant, ast.JoinedStr, ast.List, ast.Tuple),
                )
            ):
                value = node.value
            if value is not None:
                expressions.append(value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"append", "extend"}
            ):
                expressions.extend(node.args)
            for expression in expressions:
                if _contains_authored_text(expression):
                    violations.append(
                        f"{path}:{getattr(expression, 'lineno', function.lineno)}: "
                        f"untranslated human renderer {function.name}"
                    )
    return violations


def _named_message_violations(path: Path, tree: ast.Module) -> list[str]:
    """Find authored prompts, constants, and user exceptions behind names."""
    violations: list[str] = []
    scopes: list[ast.Module | ast.FunctionDef | ast.AsyncFunctionDef] = [
        tree,
        *(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
    ]
    for scope in scopes:
        nodes = _walk_scope(scope)
        authored_names: set[str] = set()
        for node in nodes:
            value = getattr(node, "value", None)
            if not isinstance(value, ast.expr) or not _contains_authored_text(value):
                continue
            if isinstance(value, ast.Call) and _call_name(value.func) in _MACHINE_RENDERERS:
                continue
            for target in _assignment_targets(node):
                if isinstance(target, ast.Name):
                    authored_names.add(target.id)

        for node in nodes:
            if not isinstance(node, ast.Call) or not node.args:
                continue
            name = _call_name(node.func)
            value = node.args[0]
            if name == "print" and isinstance(value, ast.Name) and value.id in authored_names:
                violations.append(f"{path}:{node.lineno}: untranslated named output")
            elif name == "input" and isinstance(value, ast.Name) and value.id in authored_names:
                violations.append(f"{path}:{node.lineno}: untranslated named prompt")
            elif (
                name in {"AgentExecutionError", "SystemExit"}
                and not _is_text_boundary(value)
                and (
                    _contains_authored_text(value)
                    or (isinstance(value, ast.Name) and value.id in authored_names)
                )
            ):
                violations.append(f"{path}:{node.lineno}: untranslated user exception")
    return sorted(violations, key=lambda violation: int(violation.split(":", maxsplit=2)[1]))


def test_human_renderer_guard_detects_non_literal_output_source() -> None:
    """Detect raw text behind a human-facing formatter call."""
    tree = ast.parse(
        "def format_report(items):\n"
        "    lines = [f'FAIL: {len(items)} item(s):']\n"
        "    return '\\n'.join(lines)\n"
        "print(format_report([]))\n"
    )

    assert _human_renderer_violations(Path("example.py"), tree) == [
        "example.py:2: untranslated human renderer format_report"
    ]


def test_named_message_guard_detects_prompts_constants_and_exceptions() -> None:
    """Detect authored text that passes through a local name."""
    tree = ast.parse(
        "WARNING = 'Operation timed out'\n"
        "message = 'Operation failed'\n"
        "prompt = f'Remove {42}? [y/N] '\n"
        "print(WARNING)\n"
        "print(message)\n"
        "input(prompt)\n"
        "raise SystemExit(message)\n"
        "raise AgentExecutionError(message)\n"
    )

    assert _named_message_violations(Path("example.py"), tree) == [
        "example.py:4: untranslated named output",
        "example.py:5: untranslated named output",
        "example.py:6: untranslated named prompt",
        "example.py:7: untranslated user exception",
        "example.py:8: untranslated user exception",
    ]


def test_named_message_guard_detects_async_function_output() -> None:
    """Inspect authored named output in an asynchronous lexical scope."""
    tree = ast.parse(
        "async def run():\n"
        "    message = 'Operation failed'\n"
        "    print(message)\n"
        "    raise AgentExecutionError(message)\n"
    )

    assert _named_message_violations(Path("example.py"), tree) == [
        "example.py:3: untranslated named output",
        "example.py:4: untranslated user exception",
    ]


def test_user_facing_text_crosses_localization_boundary() -> None:  # noqa: C901
    """Require explicit localization without argparse global mutation."""
    violations: list[str] = []
    for path in sorted(Path("hephaestus").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_human_renderer_violations(path, tree))
        violations.extend(_named_message_violations(path, tree))
        for node in ast.walk(tree):
            for target in _assignment_targets(node):
                if _is_argparse_translation_global(target):
                    violations.append(
                        f"{path}:{getattr(node, 'lineno', 0)}: assigns "
                        f"argparse.{getattr(target, 'attr', '')}"
                    )

            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name in _PARSER_CALLS:
                for keyword in node.keywords:
                    if keyword.arg in _DISPLAY_KEYWORDS and not _is_text_boundary(keyword.value):
                        violations.append(f"{path}:{node.lineno}: untranslated {keyword.arg}")
                if (
                    name == "add_argument_group"
                    and node.args
                    and not _is_text_boundary(node.args[0])
                ):
                    violations.append(f"{path}:{node.lineno}: untranslated argument group title")

            if (
                name == "ArgumentTypeError"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "argparse"
                and _is_direct_user_message(node)
            ):
                violations.append(f"{path}:{node.lineno}: untranslated argparse type error")

            if (
                name == "error"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "parser"
                and _is_direct_user_message(node)
            ):
                violations.append(f"{path}:{node.lineno}: untranslated parser error")

            if name == "input" and _is_direct_user_message(node):
                violations.append(f"{path}:{node.lineno}: untranslated interactive prompt")

            if name != "print" or not node.args:
                continue
            value = node.args[0]
            if not isinstance(value, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(value, ast.Constant) and value.value in {"", "\n"}:
                continue
            if (
                isinstance(value, ast.JoinedStr)
                and value.values
                and isinstance(value.values[0], ast.Constant)
                and str(value.values[0].value).startswith("::warning::")
            ):
                continue
            violations.append(f"{path}:{node.lineno}: untranslated direct output")

    assert violations == []
