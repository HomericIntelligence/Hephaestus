"""Check cyclomatic complexity against a threshold.

Wraps ``ruff check --select=C901`` to validate that no function exceeds the
maximum allowed cyclomatic complexity.

Usage::

    hephaestus-check-complexity --path mypackage/ --threshold 10
    hephaestus-check-complexity --threshold 15 --verbose
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, format_output, resolve_repo_root
from hephaestus.utils.helpers import NETWORK_TIMEOUT, get_repo_root


def run_ruff_complexity_check(
    path: str,
    threshold: int,
    repo_root: Path,
) -> list[dict[str, str]]:
    """Run ``ruff check --select=C901`` and return violations.

    Args:
        path: Path to check (relative to *repo_root*).
        threshold: Maximum allowed cyclomatic complexity.
        repo_root: Repository root directory.

    Returns:
        List of violation dicts with keys: ``file``, ``row``, ``col``,
        ``code``, ``message``.

    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select=C901",
            f"--config=lint.mccabe.max-complexity={threshold}",
            "--output-format=json",
            path,
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=NETWORK_TIMEOUT,
    )

    if not result.stdout.strip():
        return []

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    violations = []
    for item in raw:
        violations.append(
            {
                "file": item.get("filename", ""),
                "row": str(item.get("location", {}).get("row", "")),
                "col": str(item.get("location", {}).get("column", "")),
                "code": item.get("code", ""),
                "message": item.get("message", ""),
            }
        )
    return violations


def check_max_complexity(
    path: str,
    threshold: int,
    repo_root: Path | None = None,
    verbose: bool = False,
) -> bool:
    """Check that no function exceeds the complexity threshold.

    Args:
        path: Path to source directory or file to check.
        threshold: Maximum allowed cyclomatic complexity (inclusive).
        repo_root: Repository root directory. Auto-detected if None.
        verbose: Print detailed output.

    Returns:
        True if all functions are within the threshold, False otherwise.

    """
    if repo_root is None:
        repo_root = get_repo_root()

    if verbose:
        print(f"\nChecking cyclomatic complexity (threshold={threshold}) in: {path}")

    violations = run_ruff_complexity_check(path, threshold, repo_root)

    if not violations:
        print(f"\n[OK] Complexity check passed: all functions <= CC {threshold} in {path}")
        return True

    print(f"\n[FAIL] {len(violations)} function(s) exceed CC {threshold} in {path}:")
    for v in violations:
        print(f"  {v['file']}:{v['row']}:{v['col']}: {v['message']}")

    print("\nTip: Refactor using extract-method or guard-clause flattening to reduce complexity.")
    return False


def main() -> int:
    """CLI entry point for complexity checking.

    Returns:
        Exit code (0 if clean, 1 if violations found).

    """
    parser = create_validation_parser(
        "Check cyclomatic complexity against threshold",
        epilog="Example: %(prog)s --path mypackage/ --threshold 10",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=10,
        help="Maximum allowed cyclomatic complexity (default: 10)",
    )
    parser.add_argument(
        "--path",
        type=str,
        default=".",
        help="Path to source code to check (default: .)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    args = parser.parse_args()

    repo_root = resolve_repo_root(args)

    if args.json:
        violations = run_ruff_complexity_check(args.path, args.threshold, repo_root)
        report = {
            "path": args.path,
            "threshold": args.threshold,
            "violations": violations,
            "passed": not violations,
        }
        print(format_output(report, "json"))
        return 0 if not violations else 1

    success = check_max_complexity(
        path=args.path,
        threshold=args.threshold,
        repo_root=repo_root,
        verbose=args.verbose,
    )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
