"""Pre-commit CI utilities for GitHub Actions integration.

Provides two tools:

**Benchmark** (``hephaestus-bench-precommit``): Accepts elapsed time, file
count, and hook status; emits a Markdown summary table and a GitHub Actions
warning annotation if runtime exceeds the threshold.

**Version check** (``hephaestus-check-precommit-versions``): Validates that
external pre-commit hook ``rev`` values in ``.pre-commit-config.yaml`` match
the corresponding ``pixi.toml`` lower-bound versions, preventing version drift.

Usage::

    hephaestus-bench-precommit --elapsed 45 --files 300 --status passed
    hephaestus-check-precommit-versions
    hephaestus-check-precommit-versions --config .pre-commit-config.yaml --pixi pixi.toml
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# TOML loading (tomllib on 3.11+, tomli on 3.10, manual fallback)
# ---------------------------------------------------------------------------
_tomllib = None
for _mod_name in ("tomllib", "tomli"):
    try:
        _tomllib = importlib.import_module(_mod_name)
        break
    except ModuleNotFoundError:
        continue

try:
    import yaml as _yaml
except ModuleNotFoundError:
    _yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Benchmark helpers
# ---------------------------------------------------------------------------

#: Default mapping from external pre-commit repo URL to pixi.toml package name.
DEFAULT_HOOK_TO_PIXI_MAP: dict[str, str] = {
    "https://github.com/pre-commit/mirrors-mypy": "mypy",
    "https://github.com/kynan/nbstripout": "nbstripout",
    "https://github.com/pre-commit/pre-commit-hooks": "pre-commit-hooks",
}


def format_summary_table(elapsed_s: int, file_count: int, hook_status: str) -> str:
    """Format a Markdown table summarising the pre-commit benchmark run.

    Args:
        elapsed_s: Wall-clock seconds the hooks took to complete.
        file_count: Number of files processed.
        hook_status: Result string, e.g. ``"passed"`` or ``"failed"``.

    Returns:
        Markdown-formatted table string including a trailing newline.

    """
    status_icon = "✅" if hook_status == "passed" else "❌"
    return (
        "## Pre-commit Hook Benchmark\n\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        f"| Hook status | {status_icon} {hook_status} |\n"
        f"| Elapsed time | {elapsed_s}s |\n"
        f"| Files processed | {file_count} |\n"
    )


def check_threshold(elapsed_s: int, threshold_s: int = 120) -> bool:
    """Return ``True`` if the elapsed time exceeds the threshold.

    Args:
        elapsed_s: Measured runtime in seconds.
        threshold_s: Maximum acceptable runtime in seconds (default 120).

    Returns:
        ``True`` when slow (elapsed_s > threshold_s), ``False`` otherwise.

    """
    return elapsed_s > threshold_s


def emit_warning(message: str) -> None:
    """Emit a GitHub Actions warning annotation to stdout.

    Args:
        message: Warning text to emit.

    """
    print(f"::warning::{message}")


def write_step_summary(content: str, summary_path: str | None = None) -> None:
    """Append content to the GitHub Actions step summary file if the path is set.

    Args:
        content: Markdown content to write.
        summary_path: Path to the summary file; defaults to ``$GITHUB_STEP_SUMMARY``.

    """
    path = summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# Version-check helpers
# ---------------------------------------------------------------------------


def normalize_version(rev: str) -> str:
    """Strip a leading ``v`` from a git tag so it can be compared numerically.

    Args:
        rev: A git tag such as ``"v1.19.1"`` or ``"0.7.1"``.

    Returns:
        Version string without leading ``v``, e.g. ``"1.19.1"``.

    """
    return rev.lstrip("v")


def load_precommit_config(config_path: Path) -> list[dict[str, Any]]:
    """Parse ``.pre-commit-config.yaml`` and return the list of repo entries.

    Args:
        config_path: Path to ``.pre-commit-config.yaml``.

    Returns:
        List of repo dicts from the ``repos`` key.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is missing the ``repos`` key or yaml is unavailable.

    """
    if not config_path.exists():
        raise FileNotFoundError(f"Pre-commit config not found: {config_path}")

    if _yaml is None:
        raise ValueError("pyyaml is required: pip install pyyaml")

    with config_path.open() as fh:
        data = _yaml.safe_load(fh)

    if not isinstance(data, dict) or "repos" not in data:
        raise ValueError(f"No 'repos' key in {config_path}")

    repos: list[dict[str, Any]] = data["repos"]
    return repos


def extract_external_hooks(repos: list[dict[str, Any]]) -> dict[str, str]:
    """Extract external (non-local) repo URLs and their ``rev`` values.

    Args:
        repos: List of repo dicts from ``.pre-commit-config.yaml``.

    Returns:
        Dict mapping repo URL to rev string.

    """
    result: dict[str, str] = {}
    for repo in repos:
        url = repo.get("repo", "")
        rev = repo.get("rev", "")
        if url and url != "local" and rev:
            result[url] = rev
    return result


def parse_pixi_constraint(constraint: str) -> str | None:
    """Extract the lower-bound version from a pixi/conda version constraint.

    Handles patterns like:
    - ``>=1.19.1,<2``  → ``"1.19.1"``
    - ``==0.26.1``     → ``"0.26.1"``
    - ``>=0.7.1``      → ``"0.7.1"``
    - ``0.12.1``       → ``"0.12.1"`` (bare version)

    Args:
        constraint: A pixi version constraint string.

    Returns:
        Lower-bound version string, or None if unparseable.

    """
    match = re.search(r"[><=]=?\s*(\d+\.\d+[\.\d]*)", constraint)
    if match:
        return match.group(1)
    bare = re.match(r"^(\d+\.\d+[\.\d]*)$", constraint.strip())
    if bare:
        return bare.group(1)
    return None


def _is_deps_section_header(stripped: str) -> bool:
    """Return True if *stripped* is a pixi.toml section header for dependencies.

    Recognised patterns:

    - ``[dependencies]``
    - ``[feature.<name>.dependencies]``

    Args:
        stripped: A stripped TOML section header line (including brackets).

    Returns:
        True if this section contains conda/pip package→version entries.

    """
    inner = stripped.lstrip("[").split("]")[0].split("#")[0].strip()
    if inner == "dependencies":
        return True
    parts = inner.split(".")
    return len(parts) == 3 and parts[0] == "feature" and parts[2] == "dependencies"


def _parse_pixi_dependencies_fallback(pixi_path: Path) -> dict[str, str]:
    """Minimal line-by-line parser for dependency sections when tomllib is unavailable.

    Reads ``[dependencies]`` and all ``[feature.<name>.dependencies]`` sections
    and merges them into a single dict.

    Args:
        pixi_path: Path to ``pixi.toml``.

    Returns:
        Dict mapping package name (lowercased) to lower-bound version string.

    """
    deps: dict[str, str] = {}
    in_deps = False
    for line in pixi_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_deps = _is_deps_section_header(stripped)
            continue
        if in_deps and "=" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition("=")
            pkg = key.strip().lower()
            constraint = value.strip().strip('"')
            version = parse_pixi_constraint(constraint)
            if version:
                deps[pkg] = version
    return deps


def load_pixi_versions(pixi_path: Path) -> dict[str, str]:
    """Parse ``pixi.toml`` and return a dict mapping package name to lower-bound version.

    Reads ``[dependencies]`` and all ``[feature.<name>.dependencies]`` sections
    (both conda and pypi-dependencies variants are included via the TOML path).

    Args:
        pixi_path: Path to ``pixi.toml``.

    Returns:
        Dict mapping package name (lowercased) to lower-bound version string.

    Raises:
        FileNotFoundError: If the file does not exist.

    """
    if not pixi_path.exists():
        raise FileNotFoundError(f"pixi.toml not found: {pixi_path}")

    if _tomllib is not None:
        from typing import cast

        _load = _tomllib.load
        with pixi_path.open("rb") as fh:
            data = cast(dict[str, Any], _load(fh))
        deps: dict[str, str] = {}
        # Top-level [dependencies]
        for pkg, constraint in data.get("dependencies", {}).items():
            version = parse_pixi_constraint(str(constraint))
            if version:
                deps[pkg.lower()] = version
        # [feature.<name>.dependencies] and [feature.<name>.pypi-dependencies]
        for _feat_name, feat_data in data.get("feature", {}).items():
            if not isinstance(feat_data, dict):
                continue
            for section_key in ("dependencies", "pypi-dependencies"):
                for pkg, constraint in feat_data.get(section_key, {}).items():
                    version = parse_pixi_constraint(str(constraint))
                    if version:
                        deps[pkg.lower()] = version
        return deps

    return _parse_pixi_dependencies_fallback(pixi_path)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Convert a version string to a comparable tuple of integers.

    Args:
        version: Version string, e.g. ``"1.19.1"``.

    Returns:
        Tuple of ints, e.g. ``(1, 19, 1)``.

    """
    parts = []
    for part in version.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_version_drift(
    external_hooks: dict[str, str],
    pixi_versions: dict[str, str],
    hook_to_pixi_map: dict[str, str] | None = None,
) -> list[str]:
    """Compare external hook revs against pixi.toml lower-bound versions.

    Args:
        external_hooks: Dict mapping repo URL to rev (from ``.pre-commit-config.yaml``).
        pixi_versions: Dict mapping package name to lower-bound version (from ``pixi.toml``).
        hook_to_pixi_map: Custom URL→package mapping; defaults to :data:`DEFAULT_HOOK_TO_PIXI_MAP`.

    Returns:
        List of human-readable drift messages (empty if everything is consistent).

    """
    mapping = hook_to_pixi_map if hook_to_pixi_map is not None else DEFAULT_HOOK_TO_PIXI_MAP
    issues: list[str] = []
    for repo_url, rev in external_hooks.items():
        pkg_name = mapping.get(repo_url)
        if pkg_name is None:
            continue
        pixi_version = pixi_versions.get(pkg_name.lower())
        if pixi_version is None:
            issues.append(
                f"MISSING: '{pkg_name}' is used in .pre-commit-config.yaml "
                f"(rev={rev!r}) but has no entry in pixi.toml. "
                f"Add '{pkg_name} = \">={normalize_version(rev)}\"' to pixi.toml."
            )
            continue
        hook_version = normalize_version(rev)
        if _version_tuple(hook_version) != _version_tuple(pixi_version):
            issues.append(
                f"DRIFT: '{pkg_name}' — .pre-commit-config.yaml rev is "
                f"{hook_version!r} but pixi.toml lower bound is {pixi_version!r}. "
                f"They must match."
            )
    return issues


def check_version_consistency(
    precommit_path: Path | None = None,
    pixi_path: Path | None = None,
    hook_to_pixi_map: dict[str, str] | None = None,
) -> list[str]:
    """Top-level check: load both config files and return drift issues.

    Args:
        precommit_path: Path to ``.pre-commit-config.yaml`` (defaults to repo root).
        pixi_path: Path to ``pixi.toml`` (defaults to repo root).
        hook_to_pixi_map: Custom URL→package mapping; defaults to :data:`DEFAULT_HOOK_TO_PIXI_MAP`.

    Returns:
        List of drift/missing messages (empty means consistent).

    """
    from hephaestus.utils.helpers import get_repo_root

    root = get_repo_root()
    if precommit_path is None:
        precommit_path = root / ".pre-commit-config.yaml"
    if pixi_path is None:
        pixi_path = root / "pixi.toml"

    repos = load_precommit_config(precommit_path)
    external_hooks = extract_external_hooks(repos)
    pixi_versions = load_pixi_versions(pixi_path)
    return check_version_drift(external_hooks, pixi_versions, hook_to_pixi_map)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def bench_precommit_main(argv: list[str] | None = None) -> int:
    """CLI entry-point for the pre-commit benchmark helper.

    Returns:
        Always 0 — timing regressions are non-blocking.

    """
    parser = argparse.ArgumentParser(description="Report pre-commit hook benchmark results.")
    parser.add_argument("--elapsed", type=int, required=True, help="Elapsed time in seconds.")
    parser.add_argument("--files", type=int, default=0, help="Number of files processed.")
    parser.add_argument(
        "--status", default="passed", help='Hook exit status string, e.g. "passed" or "failed".'
    )
    parser.add_argument(
        "--threshold", type=int, default=120, help="Warning threshold in seconds (default: 120)."
    )

    args = parser.parse_args(argv)

    table = format_summary_table(args.elapsed, args.files, args.status)
    print(table)
    write_step_summary(table)

    if check_threshold(args.elapsed, args.threshold):
        emit_warning(
            f"Pre-commit hooks took {args.elapsed}s, "
            f"which exceeds the {args.threshold}s threshold. "
            "Consider reviewing hook configuration for performance regressions."
        )

    return 0


def check_precommit_versions_main(argv: list[str] | None = None) -> int:
    """CLI entry-point for pre-commit version drift detection.

    Returns:
        Exit code: 0 for success, 1 for drift detected or error.

    """
    parser = argparse.ArgumentParser(
        description="Check .pre-commit-config.yaml revs match pixi.toml versions"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to .pre-commit-config.yaml (default: repo root)",
    )
    parser.add_argument(
        "--pixi",
        type=Path,
        default=None,
        help="Path to pixi.toml (default: repo root)",
    )
    args = parser.parse_args(argv)

    try:
        issues = check_version_consistency(
            precommit_path=args.config,
            pixi_path=args.pixi,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if issues:
        print("Pre-commit version drift detected:")
        for issue in issues:
            print(f"  - {issue}")
        print(
            "\nFix: update the rev in .pre-commit-config.yaml or the version "
            "constraint in pixi.toml so they match."
        )
        return 1

    print("OK: all pre-commit hook versions are consistent with pixi.toml")
    return 0


if __name__ == "__main__":
    sys.exit(check_precommit_versions_main())
