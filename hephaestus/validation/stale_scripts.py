"""Detect scripts in ``scripts/`` with no usage references.

A script is considered potentially stale if no exact command operand or
Python import names its scripts-relative path in any of:

- GitHub Actions workflows and repository configuration
- root documentation and ``docs/``
- package source, tests, and other scripts (cross-references)

A command operand is a scripts-relative path invoked by python, python3, bash,
sh, source, the dot command, or sbatch. Imports are resolved from the Python
AST. Comments and string literals are not scanned as code.

``scripts/README.md`` is deliberately excluded. Its complete inventory is a
documentation invariant, not evidence that an operator or automation invokes
a script. This command reports leads for lifecycle review; it does not prove
that a script is safe to remove.

Known utility/library scripts (``common.py``, ``conftest.py``, ``__init__.py``) are
excluded from consideration.

Usage::

    hephaestus-check-stale-scripts
    hephaestus-check-stale-scripts --repo-root /path/to/repo --strict
    hephaestus-check-stale-scripts --exclude test_ --verbose

Exit codes:
    0  No stale scripts found (or warnings only without ``--strict``)
    1  Stale scripts detected (only with ``--strict``)
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

from hephaestus.cli.localization import text
from hephaestus.cli.utils import create_validation_parser, format_output, resolve_repo_root
from hephaestus.errors import boundary_main

# Scripts that are imported by other scripts (not invoked directly) — always active.
_ALWAYS_ACTIVE: frozenset[str] = frozenset(
    {
        "common.py",
        "conftest.py",
        "__init__.py",
        "setup.py",
    }
)

# Name prefixes/substrings that mark a script as always-active (e.g. pytest test files).
_ACTIVE_PATTERNS: tuple[str, ...] = ("test_", "conftest")


def _is_always_active(script_name: str) -> bool:
    """Return True if *script_name* should always be considered active.

    Args:
        script_name: Basename of the script file.

    Returns:
        True if the script is in the known-utilities set or matches an active pattern.

    """
    if script_name in _ALWAYS_ACTIVE:
        return True
    return any(pat in script_name for pat in _ACTIVE_PATTERNS)


def get_all_scripts(
    scripts_dir: Path,
    extensions: tuple[str, ...] = (".py", ".sh", ".mojo", ".sbatch"),
) -> list[str]:
    """Return paths relative to *scripts_dir* for all script files.

    Args:
        scripts_dir: Path to the ``scripts/`` directory.
        extensions: File suffixes to include.

    Returns:
        Sorted POSIX paths relative to *scripts_dir*.

    """
    return sorted(
        p.relative_to(scripts_dir).as_posix()
        for p in scripts_dir.rglob("*")
        if p.is_file() and p.suffix in extensions and not p.name.startswith(".")
    )


def get_reference_targets(repo_root: Path) -> list[Path]:
    """Collect files that may reference script names.

    Includes workflows, root configuration and documentation, package source,
    tests, other scripts, and documentation. The scripts catalog is excluded
    because merely listing a script must not suppress a lifecycle-review lead.

    Args:
        repo_root: Root of the repository.

    Returns:
        List of Path objects for files to search.

    """
    targets: list[Path] = []

    for name in (
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "justfile",
        "pyproject.toml",
        ".pre-commit-config.yaml",
    ):
        candidate = repo_root / name
        if candidate.is_file():
            targets.append(candidate)

    for directory in (".github", "docs", "hephaestus", "tests", "scripts"):
        candidate = repo_root / directory
        if candidate.is_dir():
            targets.extend(
                path
                for path in candidate.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )

    non_usage_documents = {repo_root / "scripts" / "README.md"}
    return sorted({target for target in targets if target not in non_usage_documents})


# Command names whose exact scripts-relative operand counts as usage evidence.
# python3 precedes python so the longer name is tried first.
_COMMAND_NAMES: tuple[str, ...] = ("python3", "python", "bash", "sh", "source", ".", "sbatch")

# Token types that hold prose rather than executable code in a Python file.
_NON_CODE_TOKENS: frozenset[int] = frozenset(
    token_type
    for token_type in (
        tokenize.COMMENT,
        tokenize.STRING,
        getattr(tokenize, "FSTRING_MIDDLE", None),
    )
    if token_type is not None
)

# A hash marks a comment only at the start of a line or after whitespace.
_LINE_COMMENT = re.compile(r"(?<!\S)#.*")


def _blank_python_prose(source: str) -> str:
    """Return *source* with Python comments and string literals blanked.

    Blanking keeps line and column positions. A command that sits in a comment,
    docstring, or string literal is then invisible to the command scanner.

    Args:
        source: Python source text.

    Returns:
        Source text with non-code token characters replaced by spaces.

    """
    rows = [list(line) for line in source.splitlines(keepends=True)]
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type not in _NON_CODE_TOKENS:
                continue
            start_row, start_col = token.start
            end_row, end_col = token.end
            for row_index in range(start_row - 1, end_row):
                if row_index >= len(rows):
                    break
                first = start_col if row_index == start_row - 1 else 0
                last = end_col if row_index == end_row - 1 else len(rows[row_index])
                for col in range(first, min(last, len(rows[row_index]))):
                    if rows[row_index][col] != "\n":
                        rows[row_index][col] = " "
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return ""
    return "".join("".join(row) for row in rows)


def _scannable_text(path: Path, raw: str) -> str:
    """Return text that holds only code-like command mentions.

    Python files lose comments and string literals. Other files lose line
    comments, and joined continuations place one logical command on one line.

    Args:
        path: Path of the target file.
        raw: Raw text of the target file.

    Returns:
        Text to scan for command references.

    """
    if path.suffix == ".py":
        return _blank_python_prose(raw)
    without_comments = "\n".join(_LINE_COMMENT.sub("", line) for line in raw.splitlines())
    return without_comments.replace("\\\n", " ")


def _script_referenced_by_command(script_path: str, targets: list[Path], own_path: Path) -> bool:
    """Return True if a logical command invokes the exact scripts-relative path.

    The command names are python, python3, bash, sh, source, the dot command,
    and sbatch. The operand must carry the ``scripts/`` root and may start with
    ``./``. A direct ``./scripts/...`` path also counts. Comments and string
    literals are not scanned as commands.

    Args:
        script_path: POSIX path of the script relative to ``scripts/``.
        targets: Files to search through.
        own_path: Resolved path of the script itself (excluded from search).

    Returns:
        True if an exact command operand exists in a target file.

    """
    operand = r"(?:\./)?scripts/" + re.escape(script_path)
    commands = "|".join(re.escape(name) for name in _COMMAND_NAMES)
    invoked = re.compile(r"(?<![\w./-])(?:" + commands + r")[ \t]+" + operand + r"(?![\w./-])")
    direct = re.compile(r"(?<![\w./-])\./scripts/" + re.escape(script_path) + r"(?![\w./-])")

    for target in targets:
        if target.resolve() == own_path:
            continue
        try:
            raw = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _scannable_text(target, raw)
        if invoked.search(text) or direct.search(text):
            return True
    return False


def _module_to_script_path(module: str, scripts_dir: Path) -> str | None:
    """Map a dotted module name to its scripts-relative Python path.

    Args:
        module: Dotted module name.
        scripts_dir: Path to the ``scripts/`` directory.

    Returns:
        POSIX path relative to ``scripts/``, or None if *module* is outside it.

    """
    prefix = scripts_dir.name + "."
    if not module.startswith(prefix):
        return None
    tail = module[len(prefix) :]
    if not tail:
        return None
    return tail.replace(".", "/") + ".py"


def _relative_import_matches(node: ast.ImportFrom, stem: str) -> bool:
    """Return True if a level-one relative import names the sibling stem.

    Args:
        node: ImportFrom node with level one.
        stem: Stem of the script under test.

    Returns:
        True if the relative import names the stem.

    """
    return node.module == stem or (
        node.module is None and any(alias.name == stem for alias in node.names)
    )


def _plain_import_matches(
    node: ast.Import,
    script_path: str,
    scripts_dir: Path,
    stem: str,
    sibling: bool,
) -> bool:
    """Return True if an import statement resolves to the script.

    Args:
        node: Import node to inspect.
        script_path: POSIX path of the script relative to ``scripts/``.
        scripts_dir: Path to the ``scripts/`` directory.
        stem: Stem of the script under test.
        sibling: True if the target file sits beside the script.

    Returns:
        True if the import resolves to the script.

    """
    return any(
        _module_to_script_path(alias.name, scripts_dir) == script_path
        or (sibling and alias.name == stem)
        for alias in node.names
    )


def _from_import_matches(
    node: ast.ImportFrom,
    script_path: str,
    scripts_dir: Path,
    stem: str,
    sibling: bool,
) -> bool:
    """Return True if a from-import statement resolves to the script.

    Args:
        node: ImportFrom node to inspect.
        script_path: POSIX path of the script relative to ``scripts/``.
        scripts_dir: Path to the ``scripts/`` directory.
        stem: Stem of the script under test.
        sibling: True if the target file sits beside the script.

    Returns:
        True if the import resolves to the script.

    """
    if node.level == 1 and sibling and _relative_import_matches(node, stem):
        return True
    if node.level or node.module is None:
        return False
    if _module_to_script_path(node.module, scripts_dir) == script_path:
        return True
    if any(
        _module_to_script_path(f"{node.module}.{alias.name}", scripts_dir) == script_path
        for alias in node.names
    ):
        return True
    return sibling and node.module == stem


def _script_referenced_by_python_import(
    script_path: str,
    scripts_dir: Path,
    targets: list[Path],
    own_path: Path,
) -> bool:
    """Return True if an AST import node resolves to the exact Python script.

    The three dotted spellings are ``import scripts.shell.tool``,
    ``from scripts.shell import tool``, and
    ``from scripts.shell.tool import run``. A bare or relative sibling import
    from a file in the same directory also counts.

    Args:
        script_path: POSIX path of the script relative to ``scripts/``.
        scripts_dir: Path to the ``scripts/`` directory.
        targets: Files to search through.
        own_path: Resolved path of the script itself (excluded from search).

    Returns:
        True if an exact import resolves to the script.

    """
    stem = Path(script_path).stem
    script_parent = (scripts_dir / script_path).parent.resolve()

    for target in targets:
        if target.suffix != ".py" or target.resolve() == own_path:
            continue
        try:
            raw = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError):
            continue
        sibling = target.parent.resolve() == script_parent

        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and _plain_import_matches(
                node, script_path, scripts_dir, stem, sibling
            ):
                return True
            if isinstance(node, ast.ImportFrom) and _from_import_matches(
                node, script_path, scripts_dir, stem, sibling
            ):
                return True
    return False


def find_stale_scripts(
    repo_root: Path,
    exclude_pattern: str | None = None,
) -> list[str]:
    """Return scripts-relative paths of scripts with no exact external reference.

    Scripts in ``_ALWAYS_ACTIVE`` or matching ``_ACTIVE_PATTERNS`` are excluded.
    If *exclude_pattern* is given, any script whose path contains that substring
    is also excluded.

    Args:
        repo_root: Root of the repository.
        exclude_pattern: Optional substring; matching scripts are excluded.

    Returns:
        Sorted list of possibly-stale paths relative to ``scripts/``.

    """
    scripts_dir = repo_root / "scripts"
    if not scripts_dir.is_dir():
        return []

    all_scripts = get_all_scripts(scripts_dir)
    targets = get_reference_targets(repo_root)

    stale: list[str] = []
    for script_path in all_scripts:
        script_name = Path(script_path).name
        if _is_always_active(script_name):
            continue
        if exclude_pattern and exclude_pattern in script_path:
            continue
        own_path = (scripts_dir / script_path).resolve()
        referenced = _script_referenced_by_command(
            script_path, targets, own_path
        ) or _script_referenced_by_python_import(script_path, scripts_dir, targets, own_path)
        if not referenced:
            stale.append(script_path)

    return stale


def check_stale_scripts(
    repo_root: Path,
    strict: bool = False,
    verbose: bool = False,
    exclude_pattern: str | None = None,
) -> int:
    """Run stale-script detection and return an exit code.

    Args:
        repo_root: Root of the repository.
        strict: If True, return exit code 1 when stale scripts are found.
        verbose: If True, print summary counts before results.
        exclude_pattern: Optional substring; scripts containing it are excluded.

    Returns:
        0 if no stale scripts (or in warning mode), 1 if stale scripts found and
        *strict* is True.

    """
    scripts_dir = repo_root / "scripts"

    stale = find_stale_scripts(repo_root, exclude_pattern=exclude_pattern)

    if verbose and scripts_dir.is_dir():
        all_scripts = get_all_scripts(scripts_dir)
        print(text("Total scripts: %(value0)s", value0=len(all_scripts)))
        print(text("Stale candidates: %(value0)s\n", value0=len(stale)))

    if stale:
        prefix = "ERROR" if strict else "WARNING"
        print(
            text(
                "%(value0)s: Found %(value1)s possibly stale script(s):\n",
                value0=prefix,
                value1=len(stale),
            )
        )
        for script_name in stale:
            print(text("  scripts/%(value0)s", value0=script_name))
        print(text("\nConsider removing these scripts if they are no longer needed."))
        return 1 if strict else 0

    print(text("No stale script candidates found."))
    return 0


def main() -> int:
    """Run hephaestus-check-stale-scripts through the shared console-script boundary.

    The boundary reports a failure with one exit code and one stderr line.
    A usage error still exits 2 from argparse.
    """
    return boundary_main(_run)


def _run() -> int:
    """CLI entry point for stale-script detection.

    Returns:
        Exit code (0 unless ``--strict`` and stale scripts are found).

    """
    parser = create_validation_parser(
        "Detect scripts/ files with no references in CI configs or other scripts",
        epilog="Example: %(prog)s --strict --verbose",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=text("Exit 1 when stale scripts are found (default: warn only, exit 0)"),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help=text("Print summary counts before results"),
    )
    parser.add_argument(
        "--exclude",
        metavar="PATTERN",
        default=None,
        help=text("Exclude scripts whose name contains PATTERN (e.g. 'test_')"),
    )

    args = parser.parse_args()
    repo_root = resolve_repo_root(args)

    if args.json:
        stale = find_stale_scripts(repo_root, exclude_pattern=args.exclude)
        exit_code = 1 if (stale and args.strict) else 0
        report = {
            "stale_scripts": stale,
            "stale_count": len(stale),
            "strict": args.strict,
            "exit_code": exit_code,
            "passed": not stale,
        }
        print(format_output(report, "json"))
        return exit_code

    return check_stale_scripts(
        repo_root=repo_root,
        strict=args.strict,
        verbose=args.verbose,
        exclude_pattern=args.exclude,
    )


if __name__ == "__main__":
    sys.exit(main())
