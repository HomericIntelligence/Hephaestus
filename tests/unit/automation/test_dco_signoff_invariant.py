"""Guard: every automation commit-CREATING path carries the DCO ``-s`` flag.

Executable-convention guard (issue #1606, follow-up to #1516). pr-policy
Check 3 (scripts/check_dco_signoff.py) fails any PR commit lacking a
``Signed-off-by`` trailer, produced only by ``git commit -s``. This test
converts the scattered per-path assertions into one invariant: no
commit-creating ``git commit`` / ``git cherry-pick`` / ``commit --amend``
site in the automation or github packages may omit ``-s``.

It uses an AST scan (not a text/regex scan) so it cannot misfire on
``["git", "cherry-pick", "--abort"]`` (a no-commit op) or on docstring prose
mentioning ``git commit --amend``. It also skips the inner ``Constant``
children of an f-string (``JoinedStr``) so an f-string command is counted
exactly once, never twice.

**Scope boundary (POLA)**: This guard detects only literal ``["git", ...]``
argv list literals (first element the literal string ``"git"``) and literal
``commit --amend`` string or f-string literals. Commit paths built via
variable/list concatenation, a non-literal subprocess argv, or a ``"git"``
bound to a variable are intentionally out of scope and silently escape the guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOTS = ("hephaestus/automation", "hephaestus/github")

# cherry-pick subcommands that resume/abort an in-progress pick and create no
# commit -> a list carrying one of these is NOT a commit-creating call.
_NON_CREATING = frozenset({"--abort", "--continue", "--skip", "--quit"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _py_files() -> list[Path]:
    root = _repo_root()
    files: list[Path] = []
    for sub in _ROOTS:
        files.extend((root / sub).rglob("*.py"))
    return files


def _list_of_str_constants(node: ast.AST) -> list[str | None] | None:
    """Return a list literal's elements as strings, else None.

    Non-constant elements (e.g. a variable) map to None; a non-list *node*
    yields None.
    """
    if not isinstance(node, ast.List):
        return None
    out: list[str | None] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        else:
            out.append(None)
    return out


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """id() of every Constant node that is a module/func/class docstring."""
    ids: set[int] = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body
        ):
            first = n.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _fstring_child_ids(tree: ast.AST) -> set[int]:
    """Return id() of every value node inside a JoinedStr (f-string).

    Skipping these prevents ``ast.walk`` from counting an f-string command
    twice -- once via the parent JoinedStr and again via its inner Constant
    pieces.
    """
    ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for value in n.values:
                ids.add(id(value))
    return ids


def _string_literal_text(node: ast.AST) -> str | None:
    """Return literal text of a Constant str or a JoinedStr (f-string), else None.

    For an f-string, ``{expr}`` placeholders become NUL so an interpolated
    flag can never masquerade as a static ``-s``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("\x00")
        return "".join(parts)
    return None


def _list_form_offenders(tree: ast.AST) -> list[int]:
    """Return sorted, deduped lines of commit-creating ``["git", ...]`` lists missing ``-s``."""
    offenders: set[int] = set()
    for node in ast.walk(tree):
        elts = _list_of_str_constants(node)
        if not elts or len(elts) < 2 or elts[0] != "git":
            continue
        sub = elts[1]
        present = {e for e in elts if e is not None}
        creating = sub == "commit" or (sub == "cherry-pick" and not (present & _NON_CREATING))
        if creating and "-s" not in present:
            offenders.add(node.lineno)  # type: ignore[attr-defined]
    return sorted(offenders)


def _amend_offenders(tree: ast.AST) -> list[int]:
    """Return sorted, deduped lines of non-docstring ``commit --amend`` strings missing ``-s``.

    Inner f-string Constants are skipped so a JoinedStr command is counted
    once, not once-per-piece.
    """
    skip = _docstring_node_ids(tree) | _fstring_child_ids(tree)
    offenders: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _string_literal_text(node)
        if text and "commit --amend" in text and " -s" not in text:
            offenders.add(node.lineno)  # type: ignore[attr-defined]
    return sorted(offenders)


# --- Live-tree invariant (ships green: #1516 already added -s everywhere) ---


def test_live_tree_list_form_commits_carry_signoff() -> None:
    """Every ``["git", ...]`` commit/cherry-pick in the tree carries ``-s``."""
    offenders: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path}:{ln}" for ln in _list_form_offenders(tree)]
    assert not offenders, (
        "commit-creating git commit/cherry-pick call sites missing DCO '-s' "
        "(pr-policy Check 4 will fail; issue #1606):\n" + "\n".join(offenders)
    )


def test_live_tree_amend_execs_carry_signoff() -> None:
    """Every ``commit --amend`` rewrite-exec string in the tree carries ``-s``."""
    offenders: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path}:{ln}" for ln in _amend_offenders(tree)]
    assert not offenders, (
        "commit --amend rewrite-exec strings missing DCO '-s' "
        "(issue #1606):\n" + "\n".join(offenders)
    )


# --- Detector unit tests: negative branch fires, positives are ignored ---


@pytest.mark.parametrize(
    "src",
    [
        'cmd = ["git", "commit", "-S", "-m", "x"]',  # commit, no -s
        'cmd = ["git", "cherry-pick", "-S", sha]',  # creating pick, no -s
    ],
)
def test_missing_signoff_list_is_flagged(src: str) -> None:
    """A commit-creating list literal missing ``-s`` is reported."""
    assert _list_form_offenders(ast.parse(src)) == [1]


@pytest.mark.parametrize(
    "src",
    [
        'cmd = ["git", "cherry-pick", "--abort"]',  # no-commit op
        'cmd = ["git", "cherry-pick", "-S", "-s", sha]',  # compliant
        'cmd = ["git", "commit", "-S", "-s", "-m", "x"]',  # compliant
        'cmd = ["git", "status"]',  # unrelated
    ],
)
def test_non_offending_list_is_ignored(src: str) -> None:
    """No-commit ops, compliant commits, and unrelated calls are not reported."""
    assert _list_form_offenders(ast.parse(src)) == []


def test_missing_signoff_amend_string_is_flagged() -> None:
    """A plain ``commit --amend`` string constant missing ``-s`` is reported."""
    src = 'REWRITE = "git commit --amend --no-edit -S"'  # plain Constant, no -s
    assert _amend_offenders(ast.parse(src)) == [1]


def test_amend_fstring_missing_signoff_is_flagged_once() -> None:
    """An f-string ``commit --amend`` missing ``-s`` is reported exactly once."""
    # An f-string amend missing -s must be flagged EXACTLY ONCE, not twice:
    # ast.walk yields the JoinedStr AND its inner Constants, but the inner
    # pieces are skipped via _fstring_child_ids, so the result is [1] not [1, 1].
    src = 'X = f"git -c user.email={e} commit --amend --no-edit -S"'
    assert _amend_offenders(ast.parse(src)) == [1]


def test_amend_docstring_prose_is_ignored() -> None:
    """Docstring prose mentioning ``commit --amend`` is not reported."""
    src = 'def f():\n    """Return the git commit --amend command."""\n    return 1'
    assert _amend_offenders(ast.parse(src)) == []


def test_amend_compliant_string_is_ignored() -> None:
    """A compliant ``commit --amend ... -s`` string constant is not reported."""
    assert _amend_offenders(ast.parse('R = "git commit --amend --no-edit -S -s"')) == []


def test_amend_compliant_fstring_is_ignored() -> None:
    """A compliant ``commit --amend ... -s`` f-string exec is not reported."""
    src = 'X = f"git -c e={e} commit --amend --no-edit -S -s"'
    assert _amend_offenders(ast.parse(src)) == []
