"""GitHub Actions workflow validation utilities.

Provides two checks:

**Inventory check** (``hephaestus-check-workflow-inventory``): Detects drift
between ``.github/workflows/*.yml`` files on disk and the workflow table in
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
import sys
from pathlib import Path
from typing import Any, NamedTuple

try:
    import yaml as _yaml
except ModuleNotFoundError:
    _yaml = None  # type: ignore[assignment]

# Security limit: skip workflow files larger than 1 MB
_MAX_FILE_SIZE = 1_048_576

# Matches a .yml filename (with or without a markdown hyperlink) inside a
# pipe-delimited table cell.  Examples:
#   | validate-workflows.yml |
#   | [comprehensive-tests.yml](#anchor) |
_TABLE_FILENAME_RE = re.compile(r"\|\s*\[?([a-zA-Z0-9_.-]+\.yml)\]?[^|]*\|")


# ---------------------------------------------------------------------------
# Inventory check
# ---------------------------------------------------------------------------


def collect_yml_files(repo_root: Path) -> set[str]:
    """Return basenames of ``*.yml`` files in ``.github/workflows/``, excluding worktrees.

    Args:
        repo_root: Absolute path to the repository root.

    Returns:
        Set of ``.yml`` basenames (e.g. ``{"ci.yml", "release.yml"}``).

    """
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return set()

    result: set[str] = set()
    for path in workflows_dir.glob("*.yml"):
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            rel = path
        if any(part == "worktrees" for part in rel.parts):
            continue
        result.add(path.name)
    return result


def parse_readme_table(readme_path: Path) -> set[str]:
    """Parse ``.github/workflows/README.md`` and return documented ``.yml`` filenames.

    Only lines containing a pipe-delimited table cell with a ``.yml`` filename
    are considered.  Both plain and hyperlinked forms are matched.

    Args:
        readme_path: Path to the README.md file to parse.

    Returns:
        Set of documented ``.yml`` basenames.

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
    """Compare on-disk ``.yml`` files against the README table.

    Args:
        repo_root: Absolute path to the repository root.

    Returns:
        A tuple ``(undocumented, missing_files)`` where:

        - ``undocumented``: filenames present on disk but absent from README table
        - ``missing_files``: filenames in README table but absent from disk

    """
    readme_path = repo_root / ".github" / "workflows" / "README.md"
    on_disk = collect_yml_files(repo_root)
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


def validate_workflow(workflow_file: Path) -> list[Violation]:
    """Validate checkout-first ordering for all jobs in a workflow file.

    Args:
        workflow_file: Path to the workflow YAML file.

    Returns:
        List of :class:`Violation` objects; empty list means the file passes.

    """
    if _yaml is None:
        print(f"WARNING: Skipping {workflow_file} (pyyaml not installed)", file=sys.stderr)
        return []

    if workflow_file.stat().st_size > _MAX_FILE_SIZE:
        print(
            f"WARNING: Skipping {workflow_file} (exceeds {_MAX_FILE_SIZE} byte limit)",
            file=sys.stderr,
        )
        return []

    with open(workflow_file, encoding="utf-8") as fh:
        try:
            data: Any = _yaml.safe_load(fh)
        except _yaml.YAMLError as exc:
            print(
                f"WARNING: Skipping {workflow_file} (YAML parse error: {exc})",
                file=sys.stderr,
            )
            return []

    if not isinstance(data, dict):
        return []

    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return []

    violations: list[Violation] = []
    for job_name, job_data in jobs.items():
        if not isinstance(job_data, dict):
            continue

        steps = job_data.get("steps")
        if not isinstance(steps, list):
            continue

        violations.extend(_check_job_steps(workflow_file, str(job_name), steps))

    return violations


def collect_workflow_files(paths: list[str]) -> list[Path]:
    """Expand the given paths into a list of workflow YAML files.

    Args:
        paths: File paths or directory paths. Directories are searched for
               ``*.yml`` and ``*.yaml`` files non-recursively.

    Returns:
        Deduplicated list of :class:`~pathlib.Path` objects for each candidate.

    """
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.glob("*.yml")))
            files.extend(sorted(p.glob("*.yaml")))
        else:
            print(f"WARNING: Path not found: {p}", file=sys.stderr)

    seen: set[Path] = set()
    result: list[Path] = []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def check_workflow_inventory_main() -> int:
    """CLI entry point for workflow inventory drift detection.

    Returns:
        Exit code: 0 for success, 1 for drift detected.

    """
    parser = argparse.ArgumentParser(
        description="Detect drift between .github/workflows/*.yml files and README.md table.",
        epilog="Example: %(prog)s --repo-root /path/to/repo",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect via git)",
    )
    args = parser.parse_args()

    if args.repo_root is not None:
        repo_root = args.repo_root
    else:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()

    undocumented, missing_files = check_inventory(repo_root)

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
        "so it exactly matches the *.yml files on disk."
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
    args = parser.parse_args()

    target_paths: list[str] = args.paths
    if not target_paths:
        from hephaestus.utils.helpers import get_repo_root

        repo_root = get_repo_root()
        target_paths = [str(repo_root / ".github" / "workflows")]

    workflow_files = collect_workflow_files(target_paths)

    if not workflow_files:
        print("No workflow files found to validate.")
        return 0

    all_violations: list[Violation] = []
    for wf_file in workflow_files:
        all_violations.extend(validate_workflow(wf_file))

    if all_violations:
        for v in all_violations:
            print(
                f"\nERROR: {v.workflow_file} :: job '{v.job_name}' :: step {v.step_index} "
                f"uses '{v.composite_action}'\n"
                f"       but actions/checkout is not a preceding step.\n"
                f"       Composite actions and reusable workflows require checkout first."
            )
        print(f"\nFound {len(all_violations)} violation(s) in {len(workflow_files)} file(s).")
        return 1

    print(f"OK: {len(workflow_files)} workflow file(s) checked. All pass checkout-first invariant.")
    return 0


if __name__ == "__main__":
    sys.exit(check_workflow_inventory_main())
