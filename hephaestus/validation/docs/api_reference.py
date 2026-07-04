"""Validate generated pdoc API reference output.

The release workflow publishes ``docs/api`` to GitHub Pages after running the
``docs`` Pixi task. A lazy top-level ``hephaestus`` import can generate only
``hephaestus.html`` unless pdoc is pointed at the subpackages explicitly, so
this guard verifies the generated tree contains real subpackage pages before
upload.

Usage::

    hephaestus-check-api-reference
    hephaestus-check-api-reference --docs-dir docs/api
    hephaestus-check-api-reference --json
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

PACKAGE_NAME = "hephaestus"
EXCLUDED_SUBPACKAGES = frozenset({"automation"})
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ApiReferenceFinding:
    """A single generated API-reference violation."""

    # kind values: "missing-docs-dir" | "missing-subpackage-page"
    kind: str
    detail: str


def expected_pdoc_targets(repo_root: Path, package_name: str = PACKAGE_NAME) -> tuple[str, ...]:
    """Return the pdoc targets needed to cover the package's direct subpackages.

    Args:
        repo_root: Repository root containing the package directory.
        package_name: Top-level Python package to document.

    Returns:
        Tuple beginning with ``./<package_name>`` followed by every direct
        subpackage as ``./<package_name>/<subpackage>`` in sorted order. Path
        targets force pdoc to inspect the checked-out source tree even when the
        active environment has a different editable install for the same module
        name.

    """
    package_root = repo_root / package_name
    subpackages = sorted(
        path.name
        for path in package_root.iterdir()
        if path.is_dir()
        and not path.name.startswith((".", "_"))
        and (path / "__init__.py").is_file()
        and path.name not in EXCLUDED_SUBPACKAGES
    )
    return (f"./{package_name}", *(f"./{package_name}/{name}" for name in subpackages))


def expected_direct_subpackage_pages(
    repo_root: Path = DEFAULT_REPO_ROOT,
    package_name: str = PACKAGE_NAME,
) -> tuple[str, ...]:
    """Return expected direct-subpackage page filenames for generated pdoc output."""
    return tuple(
        f"{Path(target).name}.html" for target in expected_pdoc_targets(repo_root, package_name)[1:]
    )


def list_subpackage_pages(docs_dir: Path, package_name: str = PACKAGE_NAME) -> list[Path]:
    """Return generated direct-subpackage pdoc pages under *docs_dir*.

    pdoc writes direct subpackage pages as ``docs/api/hephaestus/<name>.html``.
    Nested module pages such as ``docs/api/hephaestus/scripts_lib/helper.html``
    are intentionally excluded from this count because the near-empty failure
    mode is specifically zero direct subpackage pages.
    """
    package_dir = docs_dir / package_name
    if not package_dir.is_dir():
        return []
    return sorted(path for path in package_dir.glob("*.html") if path.is_file())


def find_violations(
    docs_dir: Path,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
) -> list[ApiReferenceFinding]:
    """Return generated API-reference violations for *docs_dir*.

    Args:
        docs_dir: Generated pdoc output directory, usually ``docs/api``.
        repo_root: Repository root used to discover the expected pdoc targets.

    Returns:
        List of findings. An empty list means the generated API reference is
        complete for every expected direct subpackage page.

    """
    if not docs_dir.is_dir():
        return [
            ApiReferenceFinding(
                kind="missing-docs-dir",
                detail=f"{docs_dir} does not exist",
            )
        ]

    generated_pages = {path.name for path in list_subpackage_pages(docs_dir)}
    missing_pages = sorted(
        page_name
        for page_name in expected_direct_subpackage_pages(repo_root)
        if page_name not in generated_pages
    )
    if missing_pages:
        return [
            ApiReferenceFinding(
                kind="missing-subpackage-page",
                detail=f"missing generated page docs/api/{PACKAGE_NAME}/{page_name}",
            )
            for page_name in missing_pages
        ]
    return []


def format_report(findings: list[ApiReferenceFinding]) -> str:
    """Render *findings* as a human-readable report."""
    if not findings:
        return "OK: generated API reference contains hephaestus subpackage pages."
    lines = [f"FAIL: {len(findings)} API-reference violation(s):"]
    lines.extend(f"  [{finding.kind}] {finding.detail}" for finding in findings)
    return "\n".join(lines)


def format_json(findings: list[ApiReferenceFinding]) -> str:
    """Render *findings* as a JSON string."""
    return json.dumps({"violations": [asdict(finding) for finding in findings]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``hephaestus-check-api-reference``."""
    parser = create_validation_parser(__doc__, prog="hephaestus-check-api-reference")
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        help="Generated API reference directory (default: <repo-root>/docs/api)",
    )
    args = parser.parse_args(argv)
    repo_root = resolve_repo_root(args)
    docs_dir = args.docs_dir if args.docs_dir is not None else repo_root / "docs" / "api"
    findings = find_violations(docs_dir, repo_root=repo_root)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
