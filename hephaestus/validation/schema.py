"""Validate YAML configuration files against JSON schemas.

Dispatches config files to the correct JSON schema based on configurable
path pattern matching. Uses ``jsonschema`` for validation.

Usage::

    hephaestus-validate-schemas config/defaults.yaml config/models/*.yaml
    hephaestus-validate-schemas --schema-map schema-map.json --verbose
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hephaestus.cli.utils import create_validation_parser, emit_json_status, resolve_repo_root

SchemaMapping = list[tuple[re.Pattern[str], Path]]


@dataclass(frozen=True)
class SchemaCheckResult:
    """Aggregate outcome of checking requested files against schemas."""

    exit_code: int
    error_count: int
    requested: int
    validated: int
    skipped: int
    passed: int
    failed: int


def load_schema_map(schema_map_file: Path) -> SchemaMapping:
    r"""Load a schema mapping from a JSON file.

    The JSON file should contain a list of ``[pattern, schema_path]`` pairs::

        [
            ["^config/defaults\\.yaml$", "schemas/defaults.schema.json"],
            ["^config/models/.+\\.yaml$", "schemas/model.schema.json"]
        ]

    Args:
        schema_map_file: Path to JSON file containing schema mappings.

    Returns:
        List of compiled ``(regex_pattern, schema_path)`` tuples.

    Raises:
        FileNotFoundError: If the schema map file does not exist.

    """
    data = json.loads(schema_map_file.read_text(encoding="utf-8"))
    return [(re.compile(pattern), Path(schema_path)) for pattern, schema_path in data]


def resolve_schema(file_path: Path, repo_root: Path, schema_map: SchemaMapping) -> Path | None:
    """Return the schema path for *file_path*, or None if no match.

    Args:
        file_path: Absolute or relative path to the config file.
        repo_root: Root of the repository (used to compute relative path).
        schema_map: List of ``(regex_pattern, schema_relative_path)`` tuples.

    Returns:
        Absolute path to the matching JSON schema file, or None.

    """
    try:
        rel = file_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = Path(str(file_path))

    rel_str = rel.as_posix()
    for pattern, schema_rel in schema_map:
        if pattern.match(rel_str):
            return repo_root / schema_rel
    return None


def validate_file(file_path: Path, schema: dict[str, object]) -> list[str]:
    """Validate a YAML config file against a JSON schema.

    Args:
        file_path: Path to the YAML config file.
        schema: Parsed JSON schema dict.

    Returns:
        List of human-readable error strings (empty means valid).

    """
    try:
        import jsonschema
    except ImportError:
        return [
            "jsonschema not installed. "
            "Install with: pip install HomericIntelligence-Hephaestus[schema]"
        ]

    try:
        with open(file_path) as fh:
            content = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        return [f"Could not read/parse YAML: {exc}"]

    errors: list[str] = []
    validator = jsonschema.Draft7Validator(schema)
    for error in sorted(validator.iter_errors(content), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "<root>"
        errors.append(f"  [{path}] {error.message}")
    return errors


def check_files(
    files: list[Path],
    repo_root: Path,
    schema_map: SchemaMapping,
    verbose: bool = False,
    dry_run: bool = False,
    allow_unmapped: bool = False,
) -> SchemaCheckResult:
    """Validate each requested file and return distinct outcome counts.

    Args:
        files: List of file paths to check.
        repo_root: Repository root for schema resolution.
        schema_map: List of ``(regex_pattern, schema_path)`` tuples.
        verbose: If True, print passing file names.
        dry_run: If True, print errors but return 0.
        allow_unmapped: If True, skip files without a schema mapping.

    Returns:
        Aggregate exit code, diagnostic count, and file outcome counts.

    """
    requested = len(files)
    validated = 0
    skipped = 0
    passed = 0
    failed = 0
    schema_cache: dict[Path, dict[str, Any]] = {}
    error_count = 0

    for file_path in files:
        schema_path = resolve_schema(file_path, repo_root, schema_map)
        if schema_path is None:
            if allow_unmapped:
                print(
                    f"WARNING: No schema mapping for {file_path} — skipping",
                    file=sys.stderr,
                )
                skipped += 1
            else:
                print(f"ERROR: No schema mapping for {file_path}", file=sys.stderr)
                failed += 1
                error_count += 1
            continue

        if schema_path not in schema_cache:
            try:
                schema_cache[schema_path] = json.loads(schema_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(
                    f"ERROR: Could not load schema {schema_path}: {exc}",
                    file=sys.stderr,
                )
                failed += 1
                error_count += 1
                continue

        validated += 1
        errors = validate_file(file_path, schema_cache[schema_path])
        if errors:
            print(f"FAIL: {file_path}", file=sys.stderr)
            for error in errors:
                print(error, file=sys.stderr)
            failed += 1
            error_count += len(errors)
        else:
            passed += 1
            if verbose:
                print(f"PASS: {file_path}")

    exit_code = 0 if dry_run or failed == 0 else 1
    return SchemaCheckResult(
        exit_code=exit_code,
        error_count=error_count,
        requested=requested,
        validated=validated,
        skipped=skipped,
        passed=passed,
        failed=failed,
    )


def main() -> int:
    """CLI entry point for config schema validation.

    Returns:
        Exit code (0 if clean or ``--dry-run``, 1 if violations found).

    """
    parser = create_validation_parser(
        "Validate config files against their JSON schemas",
        epilog="Example: %(prog)s --schema-map schemas.json config/*.yaml",
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Config files to validate",
    )
    parser.add_argument(
        "--schema-map",
        type=Path,
        default=None,
        help="JSON file defining pattern-to-schema mappings",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print passing file names",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print errors but exit 0",
    )
    parser.add_argument(
        "--allow-unmapped",
        action="store_true",
        help="Explicitly skip requested files that have no matching schema",
    )

    args = parser.parse_args()

    if not args.files:
        if args.json:
            emit_json_status(
                0,
                message="no files to validate",
                error_count=0,
                files_checked=0,
                requested=0,
                validated=0,
                skipped=0,
                passed=0,
                failed=0,
            )
        return 0

    repo_root = resolve_repo_root(args)

    if args.schema_map is None:
        if args.json:
            emit_json_status(1, message="--schema-map is required")
        else:
            print(
                "ERROR: --schema-map is required. Provide a JSON file mapping "
                "file patterns to schema paths.",
                file=sys.stderr,
            )
        return 1

    try:
        schema_map = load_schema_map(args.schema_map)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        if args.json:
            emit_json_status(1, message=f"Could not load schema map: {exc}")
        else:
            print(f"ERROR: Could not load schema map: {exc}", file=sys.stderr)
        return 1

    result = check_files(
        args.files,
        repo_root,
        schema_map,
        verbose=args.verbose,
        dry_run=args.dry_run,
        allow_unmapped=args.allow_unmapped,
    )
    if args.json:
        emit_json_status(
            result.exit_code,
            error_count=result.error_count,
            files_checked=result.validated,
            requested=result.requested,
            validated=result.validated,
            skipped=result.skipped,
            passed=result.passed,
            failed=result.failed,
            dry_run=args.dry_run,
        )
    else:
        print(
            "Summary: "
            f"requested={result.requested}, "
            f"validated={result.validated}, "
            f"skipped={result.skipped}, "
            f"passed={result.passed}, "
            f"failed={result.failed}"
        )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
