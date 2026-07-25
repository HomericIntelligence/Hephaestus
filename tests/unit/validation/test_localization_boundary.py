"""Architectural guard for Hephaestus-authored user-facing text."""

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


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_text_boundary(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        return _call_name(node.func) == "text"
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.IfExp):
        return _is_text_boundary(node.body) and _is_text_boundary(node.orelse)
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "argparse"
            and node.attr == "SUPPRESS"
        )
    return False


def _is_argparse_translation_global(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "argparse"
        and node.attr in {"_", "ngettext"}
    )


def _is_direct_user_message(node: ast.Call) -> bool:
    """Return whether a call carries a literal user-facing message."""
    if not node.args:
        return False
    return isinstance(node.args[0], (ast.Constant, ast.JoinedStr)) and not _is_text_boundary(
        node.args[0]
    )


def _assignment_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return [node.target]
    return []


def test_user_facing_text_crosses_localization_boundary() -> None:  # noqa: C901
    """Literal CLI/output text is localized without process-global mutation."""
    violations: list[str] = []
    for path in sorted(Path("hephaestus").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
                # GitHub Actions workflow-command protocol, not human prose.
                continue
            violations.append(f"{path}:{node.lineno}: untranslated direct output")

    assert violations == []
