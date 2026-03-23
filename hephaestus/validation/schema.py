"""Validate YAML configuration files against JSON schemas.

Dispatches config files to the correct JSON schema based on configurable
path pattern matching. Uses ``jsonschema`` for validation.

Usage::

    hephaestus-validate-schemas config/defaults.yaml config/models/*.yaml
    hephaestus-validate-schemas --schema-map schema-map.json --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

SchemaMapping = list[tuple[re.Pattern[str], Path]]


def load_schema_map(schema_map_file: Path) -> SchemaMapping:
    """Load a schema mapping from a JSON file.

    The JSON file should contain a list of ``[pattern, schema_path]`` pairs::

        [
            ["^config/defaults\\\\.yaml$", "schemas/defaults.schema.json"],
            ["^config/models/.+\\\\.yaml$", "schemas/model.schema.json"]
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


def resolve_schema(
    file_path: Path, repo_root: Path, schema_map: SchemaMapping
) -> Path | None:
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
) -> tuple[int, int]:
    """Validate each file against its matching schema.

    Args:
        files: List of file paths to check.
        repo_root: Repository root for schema resolution.
        schema_map: List of ``(regex_pattern, schema_path)`` tuples.
        verbose: If True, print passing file names.
        dry_run: If True, print errors but return 0.

    Returns:
        Tuple of ``(exit_code, error_count)``.

    """
    if not files:
        return 0, 0

    schema_cache: dict[Path, dict[str, Any]] = {}
    any_failure = False
    error_count = 0

    for file_path in files:
        schema_path = resolve_schema(file_path, repo_root, schema_map)
        if schema_path is None:
            print(
                f"WARNING: No schema mapping for {file_path} — skipping",
                file=sys.stderr,
            )
            continue

        if schema_path not in schema_cache:
            try:
                schema_cache[schema_path] = json.loads(
                    schema_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                print(
                    f"ERROR: Could not load schema {schema_path}: {exc}",
                    file=sys.stderr,
                )
                any_failure = True
                error_count += 1
                continue

        errors = validate_file(file_path, schema_cache[schema_path])
        if errors:
            print(f"FAIL: {file_path}", file=sys.stderr)
            for error in errors:
                print(error, file=sys.stderr)
            any_failure = True
            error_count += len(errors)
        elif verbose:
            print(f"PASS: {file_path}")

    if any_failure and dry_run:
        return 0, error_count
    return (1 if any_failure else 0), error_count


def main() -> int:
    """CLI entry point for config schema validation.

    Returns:
        Exit code (0 if clean or ``--dry-run``, 1 if violations found).

    """
    parser = argparse.ArgumentParser(
        description="Validate config files against their JSON schemas",
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
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print errors but exit 0",
    )

    args = parser.parse_args()

    if not args.files:
        return 0

    from hephaestus.utils.helpers import get_repo_root as _get_repo_root

    repo_root = args.repo_root or _get_repo_root()

    if args.schema_map is None:
        print(
            "ERROR: --schema-map is required. Provide a JSON file mapping "
            "file patterns to schema paths.",
            file=sys.stderr,
        )
        return 1

    try:
        schema_map = load_schema_map(args.schema_map)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: Could not load schema map: {exc}", file=sys.stderr)
        return 1

    exit_code, _ = check_files(
        args.files, repo_root, schema_map, verbose=args.verbose, dry_run=args.dry_run
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
