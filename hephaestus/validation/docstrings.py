"""Check Python docstrings for genuine sentence fragments.

Parses Python source files using the ``ast`` module to extract docstrings,
then validates that the first sentence of each docstring is not a genuine
fragment (i.e. starts with a lowercase continuation word like ``"across"``,
``"and"``, ``"or"``).

Usage::

    hephaestus-check-docstrings --directory mypackage/
    hephaestus-check-docstrings --verbose --json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root

_CONTINUATION_STARTERS = frozenset(
    {
        "according", "across", "after", "against", "along", "alongside",
        "also", "although", "among", "and", "around", "as", "at", "based",
        "because", "before", "beneath", "beside", "between", "beyond",
        "but", "by", "compared", "depending", "despite", "during", "except",
        "following", "for", "from", "given", "hence", "however", "if", "in",
        "including", "instead", "into", "nor", "of", "on", "or", "otherwise",
        "over", "per", "plus", "relative", "since", "so", "than", "that",
        "the", "then", "thereby", "therefore", "though", "through",
        "throughout", "thus", "to", "toward", "under", "unless", "until",
        "upon", "using", "via", "when", "where", "whereas", "whether",
        "which", "while", "with", "within", "without", "yet",
    }
)


@dataclass
class FragmentFinding:
    """A genuine docstring fragment found in a Python source file."""

    file: str
    line: int
    docstring_first_line: str
    context: str

    def format(self) -> str:
        """Return a human-readable description of this finding."""
        return (
            f"  {self.file}:{self.line}\n"
            f"    Context: {self.context}\n"
            f"    First line: {self.docstring_first_line!r}\n"
        )


def is_genuine_fragment(docstring: str) -> bool:
    """Return True if the docstring's first line is a genuine sentence fragment.

    A genuine fragment is detected when the first non-empty line starts with a
    lowercase continuation word.

    Args:
        docstring: The docstring text.

    Returns:
        True if the docstring is a genuine fragment.

    """
    lines = docstring.splitlines()
    first_line = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line:
        return False

    first_word = first_line.split()[0].rstrip(".,;:!?")
    if first_word and first_word == first_word.lower() and first_word.isalpha():
        return first_word in _CONTINUATION_STARTERS

    return False


def _context_label(node: ast.AST) -> str:
    """Return a human-readable label for the AST node containing a docstring."""
    if isinstance(node, ast.Module):
        return "module"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"def {node.name}"
    return "unknown"


def _docstring_nodes(tree: ast.Module) -> list[tuple[ast.AST, str, int]]:
    """Extract ``(node, docstring_text, line_number)`` for all docstring-bearing nodes."""
    results: list[tuple[ast.AST, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first_stmt = body[0]
        if not isinstance(first_stmt, ast.Expr):
            continue
        value = first_stmt.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        results.append((node, value.value, first_stmt.lineno))
    return results


def scan_file(file_path: Path, repo_root: Path) -> list[FragmentFinding]:
    """Scan a single Python file and return genuine fragment findings.

    Args:
        file_path: Path to the Python file.
        repo_root: Repository root for computing relative paths.

    Returns:
        List of :class:`FragmentFinding` instances.

    """
    findings: list[FragmentFinding] = []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return findings

    try:
        relative_path = str(file_path.relative_to(repo_root))
    except ValueError:
        relative_path = str(file_path)

    for node, docstring, lineno in _docstring_nodes(tree):
        if is_genuine_fragment(docstring):
            first_line = next(
                (ln.strip() for ln in docstring.splitlines() if ln.strip()), ""
            )
            findings.append(
                FragmentFinding(
                    file=relative_path,
                    line=lineno,
                    docstring_first_line=first_line,
                    context=_context_label(node),
                )
            )
    return findings


def scan_directory(directory: Path, repo_root: Path) -> list[FragmentFinding]:
    """Scan all Python files under a directory for docstring fragments.

    Args:
        directory: Directory to scan.
        repo_root: Repository root for computing relative paths.

    Returns:
        List of all :class:`FragmentFinding` instances found.

    """
    all_findings: list[FragmentFinding] = []
    for py_file in sorted(directory.rglob("*.py")):
        all_findings.extend(scan_file(py_file, repo_root))
    return all_findings


def format_report(findings: list[FragmentFinding]) -> str:
    """Format findings as a human-readable text report.

    Args:
        findings: List of fragment findings.

    Returns:
        Formatted report string.

    """
    if not findings:
        return "No docstring fragment violations found.\n"

    lines: list[str] = [f"Found {len(findings)} genuine docstring fragment(s):", ""]
    for f in findings:
        lines.append(f.format())
    return "\n".join(lines)


def format_json(findings: list[FragmentFinding]) -> str:
    """Format findings as a JSON string.

    Args:
        findings: List of fragment findings.

    Returns:
        JSON string.

    """
    return json.dumps([asdict(f) for f in findings], indent=2)


def main() -> int:
    """CLI entry point for docstring fragment checking.

    Returns:
        Exit code (0 if no violations, 1 if violations found).

    """
    parser = argparse.ArgumentParser(
        description="Check Python docstrings for genuine sentence fragments",
        epilog="Example: %(prog)s --directory mypackage/",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Directory to scan (default: auto-detect source package)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed output",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )

    args = parser.parse_args()
    repo_root = args.repo_root or get_repo_root()
    directory = args.directory or repo_root

    findings = scan_directory(directory, repo_root)

    if args.json_output:
        print(format_json(findings))
    else:
        print(format_report(findings))

    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
