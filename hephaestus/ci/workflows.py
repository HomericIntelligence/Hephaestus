"""GitHub Actions workflow validation utilities.

Provides two checks:

**Inventory check** (``hephaestus-check-workflow-inventory``): Detects drift
between ``.github/workflows/*.yml`` and ``*.yaml`` files on disk and the
workflow table in
``.github/workflows/README.md``.

**Checkout-order check** (``hephaestus-validate-workflow-checkout``): Validates
that composite action and reusable workflow references
(``uses: ./.github/actions/X`` or ``uses: ./.github/workflows/X``) are always
preceded by an ``actions/checkout`` step within the same job.

Usage::

    hephaestus-check-workflow-inventory
    hephaestus-check-workflow-inventory --repo-root /path/to/repo
    hephaestus-validate-workflow-checkout
    hephaestus-validate-workflow-checkout .github/workflows/ci.yml
"""

from __future__ import annotations

import argparse
import re
import stat
import sys
from pathlib import Path
from typing import Any, NamedTuple

from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status, format_output

_yaml: Any | None = None
try:
    import yaml as _pyyaml
except ModuleNotFoundError:
    pass
else:
    _yaml = _pyyaml

# Security limit: reject workflow files larger than 1 MB.
_MAX_FILE_SIZE = 1_048_576
_WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})

# Matches a .yml or .yaml filename (with or without a markdown hyperlink) inside a
# pipe-delimited table cell.  Examples:
#   | validate-workflows.yml |
#   | [comprehensive-tests.yml](#anchor) |
_TABLE_FILENAME_RE = re.compile(r"\|\s*\[?([a-zA-Z0-9_.-]+\.ya?ml)\]?[^|]*\|")


# ---------------------------------------------------------------------------
# Inventory check
# ---------------------------------------------------------------------------


def collect_yml_files(repo_root: Path) -> set[str]:
    """Return basenames of workflow files in ``.github/workflows/``.

    This compatibility wrapper delegates workflow discovery to
    :func:`collect_workflow_files`.

    Args:
        repo_root: Absolute path to the repository root.

    Returns:
        Set of ``.yml`` and ``.yaml`` basenames (e.g.
        ``{"ci.yml", "release.yaml"}``).

    """
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return set()
    return {path.name for path in collect_workflow_files([str(workflows_dir)])}


def parse_readme_table(readme_path: Path) -> set[str]:
    """Parse the workflow README and return documented workflow filenames.

    Only lines containing a pipe-delimited table cell with a ``.yml`` or
    ``.yaml`` filename are considered.  Both plain and hyperlinked forms are
    matched.

    Args:
        readme_path: Path to the README.md file to parse.

    Returns:
        Set of documented ``.yml`` and ``.yaml`` basenames.

    """
    if not readme_path.is_file():
        return set()

    content = readme_path.read_text(encoding="utf-8")
    found: set[str] = set()
    for line in content.splitlines():
        for match in _TABLE_FILENAME_RE.finditer(line):
            found.add(match.group(1))
    return found


def check_inventory(repo_root: Path) -> tuple[list[str], list[str]]:
    """Compare on-disk workflow files against the README table.

    Args:
        repo_root: Absolute path to the repository root.

    Returns:
        A tuple ``(undocumented, missing_files)`` where:

        - ``undocumented``: filenames present on disk but absent from README table
        - ``missing_files``: filenames in README table but absent from disk

    """
    readme_path = repo_root / ".github" / "workflows" / "README.md"
    on_disk = {path.name for path in collect_workflow_files([str(readme_path.parent)])}
    in_readme = parse_readme_table(readme_path)

    undocumented = sorted(on_disk - in_readme)
    missing_files = sorted(in_readme - on_disk)
    return undocumented, missing_files


# ---------------------------------------------------------------------------
# Checkout-order check
# ---------------------------------------------------------------------------


class Violation(NamedTuple):
    """A single checkout-order violation."""

    workflow_file: Path
    job_name: str
    step_index: int
    step_name: str
    composite_action: str


class WorkflowToolError(NamedTuple):
    """A failure that prevented a workflow target from being validated."""

    target: Path
    code: str
    message: str


class WorkflowValidationError(RuntimeError):
    """Raised when workflow discovery or validation is incomplete."""

    def __init__(
        self,
        errors: list[WorkflowToolError],
        workflow_files: list[Path] | None = None,
    ) -> None:
        """Initialize the error with every incomplete-validation finding."""
        self.errors: tuple[WorkflowToolError, ...] = tuple(errors)
        self.workflow_files: tuple[Path, ...] = tuple(workflow_files or ())
        detail = "; ".join(f"{error.target}: {error.message}" for error in errors)
        super().__init__(detail)


class _WorkflowCollectionResult(NamedTuple):
    workflow_files: list[Path]
    tool_errors: list[WorkflowToolError]


class _WorkflowCheckResult(NamedTuple):
    violations: list[Violation]
    tool_errors: list[WorkflowToolError]


def _is_checkout_step(step: object) -> bool:
    """Return True if the step uses ``actions/checkout`` (any version or hash).

    Args:
        step: A step dict from a workflow YAML.

    Returns:
        True if the step is an actions/checkout step.

    """
    if not isinstance(step, dict):
        return False
    uses = step.get("uses", "")
    return isinstance(uses, str) and uses.startswith("actions/checkout")


def _is_local_reference_step(step: object) -> bool:
    """Return True if the step references a local composite action or reusable workflow.

    Args:
        step: A step dict from a workflow YAML.

    Returns:
        True if the step uses a local ``./.github/actions/`` or ``./.github/workflows/`` ref.

    """
    if not isinstance(step, dict):
        return False
    uses = step.get("uses", "")
    if not isinstance(uses, str):
        return False
    return uses.startswith("./.github/actions/") or uses.startswith("./.github/workflows/")


def _check_job_steps(workflow_file: Path, job_name: str, steps: list[Any]) -> list[Violation]:
    """Check a single job's steps for checkout-first ordering violations.

    Args:
        workflow_file: Path to the workflow YAML file (for error reporting).
        job_name: Name of the job being checked.
        steps: List of step dicts from the job.

    Returns:
        List of :class:`Violation` objects found in this job.

    """
    violations: list[Violation] = []
    checked_out = False
    for idx, step in enumerate(steps):
        if _is_checkout_step(step):
            checked_out = True
            continue
        if _is_local_reference_step(step) and not checked_out:
            step_name = (
                step.get("name", f"(unnamed step {idx + 1})")
                if isinstance(step, dict)
                else f"(step {idx + 1})"
            )
            composite_action = step.get("uses", "") if isinstance(step, dict) else ""
            violations.append(
                Violation(
                    workflow_file=workflow_file,
                    job_name=str(job_name),
                    step_index=idx + 1,
                    step_name=str(step_name),
                    composite_action=str(composite_action),
                )
            )
    return violations


def _read_workflow_document(
    workflow_file: Path,
) -> tuple[Any, WorkflowToolError | None]:
    """Read and parse a workflow, returning a typed tool failure when needed."""
    if _yaml is None:
        return None, WorkflowToolError(
            workflow_file,
            "dependency_unavailable",
            "PyYAML is not installed",
        )

    try:
        file_size = workflow_file.stat().st_size
    except OSError as exc:
        return None, WorkflowToolError(workflow_file, "stat_error", f"cannot stat file: {exc}")

    if file_size > _MAX_FILE_SIZE:
        return None, WorkflowToolError(
            workflow_file,
            "oversized",
            f"file exceeds {_MAX_FILE_SIZE} byte limit",
        )

    try:
        with workflow_file.open(encoding="utf-8") as fh:
            data: Any = _yaml.safe_load(fh)
    except _yaml.YAMLError as exc:
        return None, WorkflowToolError(workflow_file, "yaml_parse", f"YAML parse error: {exc}")
    except UnicodeError as exc:
        return None, WorkflowToolError(workflow_file, "decode_error", f"cannot decode UTF-8: {exc}")
    except OSError as exc:
        return None, WorkflowToolError(workflow_file, "read_error", f"cannot read file: {exc}")
    return data, None


def _validate_workflow_detailed(workflow_file: Path) -> _WorkflowCheckResult:
    """Validate one workflow and retain tool failures for CLI aggregation."""
    data, tool_error = _read_workflow_document(workflow_file)
    if tool_error is not None:
        return _WorkflowCheckResult([], [tool_error])

    if data is None:
        return _WorkflowCheckResult(
            [],
            [WorkflowToolError(workflow_file, "empty_document", "YAML document is empty")],
        )
    if not isinstance(data, dict):
        return _WorkflowCheckResult(
            [],
            [
                WorkflowToolError(
                    workflow_file,
                    "invalid_document",
                    "YAML document root must be a mapping",
                )
            ],
        )

    violations: list[Violation] = []
    jobs = data.get("jobs")
    if isinstance(jobs, dict):
        for job_name, job_data in jobs.items():
            if not isinstance(job_data, dict):
                continue

            steps = job_data.get("steps")
            if isinstance(steps, list):
                violations.extend(_check_job_steps(workflow_file, str(job_name), steps))

    return _WorkflowCheckResult(violations, [])


def validate_workflow(workflow_file: Path) -> list[Violation]:
    """Validate checkout-first ordering for all jobs in a workflow file.

    Args:
        workflow_file: Path to the workflow YAML file.

    Returns:
        List of :class:`Violation` objects; empty list means the file passes.

    Raises:
        WorkflowValidationError: If the file could not be completely validated.

    """
    result = _validate_workflow_detailed(workflow_file)
    if result.tool_errors:
        raise WorkflowValidationError(result.tool_errors)
    return result.violations


def _collect_workflow_files_detailed(paths: list[str]) -> _WorkflowCollectionResult:
    """Collect workflow files and retain discovery failures for CLI aggregation."""
    files: list[Path] = []
    tool_errors: list[WorkflowToolError] = []

    for raw in paths:
        target = Path(raw)
        try:
            mode = target.stat().st_mode
        except OSError as exc:
            tool_errors.append(
                WorkflowToolError(
                    target,
                    "target_stat_error",
                    f"cannot inspect target: {exc}",
                )
            )
            continue

        if stat.S_ISREG(mode):
            if target.suffix in _WORKFLOW_SUFFIXES:
                files.append(target)
        elif stat.S_ISDIR(mode):
            try:
                directory_entries = list(target.iterdir())
                workflow_files = sorted(
                    path for path in directory_entries if path.suffix in _WORKFLOW_SUFFIXES
                )
            except OSError as exc:
                tool_errors.append(
                    WorkflowToolError(
                        target,
                        "directory_read_error",
                        f"cannot enumerate directory: {exc}",
                    )
                )
            else:
                files.extend(workflow_files)
        else:
            tool_errors.append(
                WorkflowToolError(
                    target,
                    "unsupported_target",
                    "target is neither a regular file nor a directory",
                )
            )

    seen: set[Path] = set()
    deduplicated: list[Path] = []
    for workflow_file in files:
        try:
            key = workflow_file.resolve()
        except (OSError, RuntimeError) as exc:
            tool_errors.append(
                WorkflowToolError(
                    workflow_file,
                    "target_resolve_error",
                    f"cannot resolve target: {exc}",
                )
            )
            continue
        if key not in seen:
            seen.add(key)
            deduplicated.append(workflow_file)

    return _WorkflowCollectionResult(deduplicated, tool_errors)


def collect_workflow_files(paths: list[str]) -> list[Path]:
    """Expand the given paths into a list of workflow YAML files.

    Args:
        paths: File paths or directory paths. Directories are searched for
               ``*.yml`` and ``*.yaml`` files non-recursively.

    Returns:
        Deduplicated list of :class:`~pathlib.Path` objects for each candidate.

    Raises:
        WorkflowValidationError: If any requested target cannot be inspected.

    """
    result = _collect_workflow_files_detailed(paths)
    if result.tool_errors:
        raise WorkflowValidationError(result.tool_errors, result.workflow_files)
    return result.workflow_files


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def check_workflow_inventory_main() -> int:
    """CLI entry point for workflow inventory drift detection.

    Returns:
        Exit code: 0 for success, 1 for drift detected.

    """
    parser = argparse.ArgumentParser(
        description=(
            "Detect drift between .github/workflows/*.yml and *.yaml files and README.md table."
        ),
        epilog="Example: %(prog)s --repo-root /path/to/repo",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect via git)",
    )
    add_json_arg(parser)
    add_version_arg(parser)
    args = parser.parse_args()

    if args.repo_root is not None:
        repo_root = args.repo_root
    else:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()

    undocumented, missing_files = check_inventory(repo_root)

    if args.json:
        in_sync = not undocumented and not missing_files
        payload = {
            "in_sync": in_sync,
            "undocumented": list(undocumented),
            "missing_files": list(missing_files),
        }
        print(format_output(payload, "json"))
        return 0 if in_sync else 1

    if not undocumented and not missing_files:
        print("OK: workflow inventory is in sync.")
        return 0

    print("ERROR: workflow inventory drift detected!\n")

    if undocumented:
        print("Files on disk but NOT documented in .github/workflows/README.md:")
        for name in undocumented:
            print(f"  + {name}")
        print()

    if missing_files:
        print("Files documented in README.md table but NOT present on disk:")
        for name in missing_files:
            print(f"  - {name}")
        print()

    print(
        "Fix: update the Workflow Summary table in .github/workflows/README.md "
        "so it exactly matches the *.yml and *.yaml files on disk."
    )
    return 1


def validate_workflow_checkout_main() -> int:
    """CLI entry point for checkout-order validation.

    Returns:
        Exit code: 0 for success, 1 for violations found.

    """
    parser = argparse.ArgumentParser(
        description="Validate that composite actions are preceded by actions/checkout.",
        epilog="Example: %(prog)s .github/workflows/ci.yml",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Workflow files or directories (default: .github/workflows/)",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow readable requested targets to contain no workflow YAML files",
    )
    add_json_arg(parser)
    add_version_arg(parser)
    args = parser.parse_args()

    target_paths: list[str] = args.paths
    if not target_paths:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()
        target_paths = [str(repo_root / ".github" / "workflows")]

    try:
        workflow_files = collect_workflow_files(target_paths)
        tool_errors: list[WorkflowToolError] = []
    except WorkflowValidationError as exc:
        workflow_files = list(exc.workflow_files)
        tool_errors = list(exc.errors)
    all_violations: list[Violation] = []
    for wf_file in workflow_files:
        result = _validate_workflow_detailed(wf_file)
        all_violations.extend(result.violations)
        tool_errors.extend(result.tool_errors)

    policy_violations: list[dict[str, Any]] = [
        {
            "code": "checkout_order",
            "workflow_file": str(violation.workflow_file),
            "job_name": violation.job_name,
            "step_index": violation.step_index,
            "step_name": violation.step_name,
            "composite_action": violation.composite_action,
        }
        for violation in all_violations
    ]

    empty_inventory = not workflow_files and not tool_errors
    if empty_inventory and not args.allow_empty:
        policy_violations.append(
            {
                "code": "empty_inventory",
                "targets": target_paths,
                "message": "no workflow files found in requested targets",
            }
        )

    exit_code = 1 if tool_errors or policy_violations else 0

    if args.json:
        emit_json_status(
            exit_code,
            message="workflow validation failed" if exit_code else "workflow validation passed",
            files_checked=len(workflow_files),
            violation_count=len(all_violations),
            policy_violation_count=len(policy_violations),
            tool_error_count=len(tool_errors),
            policy_violations=policy_violations,
            tool_errors=[
                {
                    "target": str(error.target),
                    "code": error.code,
                    "message": error.message,
                }
                for error in tool_errors
            ],
        )
        return exit_code

    for error in tool_errors:
        print(
            f"TOOL ERROR [{error.code}]: {error.target}: {error.message}",
            file=sys.stderr,
        )

    if empty_inventory and not args.allow_empty:
        print("POLICY VIOLATION [empty_inventory]: no workflow files found to validate.")

    for violation in all_violations:
        print(
            f"POLICY VIOLATION [checkout_order]: {violation.workflow_file} :: "
            f"job '{violation.job_name}' :: step {violation.step_index} "
            f"uses '{violation.composite_action}' before actions/checkout."
        )

    if exit_code:
        return 1

    if empty_inventory:
        print("No workflow files found to validate.")
        return 0

    print(f"OK: {len(workflow_files)} workflow file(s) checked. All pass checkout-first invariant.")
    return 0


if __name__ == "__main__":
    sys.exit(check_workflow_inventory_main())
