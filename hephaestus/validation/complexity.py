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

from hephaestus.cli.utils import (
    create_validation_parser,
    emit_json_status,
    format_output,
    resolve_repo_root,
)
from hephaestus.config.child_environments import build_python_phase_env
from hephaestus.utils.helpers import NETWORK_TIMEOUT, get_repo_root


class RuffComplexityError(RuntimeError):
    """Raised when Ruff cannot produce a trustworthy complexity report."""

    def __init__(
        self,
        message: str,
        *,
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        """Initialize a Ruff tool failure."""
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


def _subprocess_text(value: str | bytes | None) -> str:
    """Return subprocess exception output as display-safe text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


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

    Raises:
        RuffComplexityError: If the target is missing or Ruff does not return
            a successful, valid JSON report.

    """
    target = Path(path)
    if not target.is_absolute():
        target = repo_root / target
    if not target.exists():
        raise RuffComplexityError(f"Ruff target does not exist: {target}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--select=C901",
                f"--config=lint.mccabe.max-complexity={threshold}",
                "--output-format=json",
                "--exit-zero",
                path,
            ],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=NETWORK_TIMEOUT,
            env=build_python_phase_env(repo_root),
        )
    except subprocess.TimeoutExpired as exc:
        timeout = exc.timeout if exc.timeout is not None else NETWORK_TIMEOUT
        raise RuffComplexityError(
            f"Ruff timed out after {timeout} seconds",
            stderr=_subprocess_text(exc.stderr).strip(),
        ) from exc
    except OSError as exc:
        raise RuffComplexityError(
            f"Failed to launch Ruff: {exc}",
            stderr=str(exc),
        ) from exc

    stderr = result.stderr.strip()
    if result.returncode != 0:
        raise RuffComplexityError(
            f"Ruff failed with exit code {result.returncode}",
            stderr=stderr,
            returncode=result.returncode,
        )

    output = result.stdout.strip()
    if not output:
        raise RuffComplexityError(
            "Ruff returned empty JSON output",
            stderr=stderr,
        )

    try:
        raw = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuffComplexityError(
            f"Ruff returned invalid JSON output: {exc}",
            stderr=stderr,
        ) from exc

    if not isinstance(raw, list):
        raise RuffComplexityError(
            "Ruff returned invalid JSON output: expected a JSON array",
            stderr=stderr,
        )

    violations: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RuffComplexityError(
                f"Ruff returned invalid JSON output: finding {index} is not an object",
                stderr=stderr,
            )
        location = item.get("location", {})
        if not isinstance(location, dict):
            raise RuffComplexityError(
                f"Ruff returned invalid JSON output: finding {index} has an invalid location",
                stderr=stderr,
            )
        violations.append(
            {
                "file": item.get("filename", ""),
                "row": str(location.get("row", "")),
                "col": str(location.get("column", "")),
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

    try:
        violations = run_ruff_complexity_check(path, threshold, repo_root)
    except RuffComplexityError as exc:
        print(f"\n[ERROR] Complexity check failed: {exc}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return False

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
        try:
            violations = run_ruff_complexity_check(args.path, args.threshold, repo_root)
        except RuffComplexityError as exc:
            emit_json_status(
                1,
                message=str(exc),
                stderr=exc.stderr,
                ruff_exit_code=exc.returncode,
            )
            return 1

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
