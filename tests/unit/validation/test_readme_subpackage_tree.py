"""Guards for README root navigation and the AGENTS.md package inventory."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "hephaestus"
README = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

NAVIGATION_HEADING = "## Repository navigation"
NAVIGATION_EXCLUSIONS = {
    "README.md": "The navigation index lives in README.md, so a self-link adds no value.",
}
TABLE_TARGET_RE = re.compile(r"^\|\s*\[[^\]]+\]\(([^)]+)\)\s*\|", re.MULTILINE)
TREE_MARKER_RE = re.compile(r"^\s*(?:├──|└──|(?:\|--|`--|\+--)\s+\S)")


def _real_subpackages() -> set[str]:
    """Return the names of every importable hephaestus/ subpackage on disk."""
    return {
        p.name
        for p in PACKAGE_DIR.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith("__")
    }


def _tracked_top_level_entries() -> set[str]:
    """Return every top-level path represented by a tracked repository file."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        raw_path.decode("utf-8").split("/", 1)[0]
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    }


def _navigation_section() -> str:
    """Return the README repository-navigation section."""
    readme = README.read_text(encoding="utf-8")
    _, separator, remainder = readme.partition(f"{NAVIGATION_HEADING}\n")
    assert separator, f"README is missing {NAVIGATION_HEADING!r}"
    section, _, _ = remainder.partition("\n## ")
    return section


def _navigation_table_targets(section: str | None = None) -> list[str]:
    """Return normalized targets from the navigation table's Path column only."""
    if section is None:
        section = _navigation_section()
    return [
        raw_target.strip().strip("<>").split("#", 1)[0].rstrip("/").removeprefix("./")
        for raw_target in TABLE_TARGET_RE.findall(section)
    ]


def _assert_exact_root_navigation_inventory(targets: list[str]) -> None:
    """Require navigation rows to be the exact tracked root-path inventory."""
    non_root_targets = sorted(
        target
        for target in targets
        if not target or target in {".", ".."} or len(Path(target).parts) != 1
    )
    assert not non_root_targets, (
        "README repository navigation must use direct top-level targets, not nested "
        f"or invalid paths: {non_root_targets}"
    )

    expected = _tracked_top_level_entries() - set(NAVIGATION_EXCLUSIONS)
    actual = set(targets)
    missing = sorted(expected - actual)
    stale = sorted(actual - expected)
    assert actual == expected, (
        "README repository navigation must be the exact tracked root-path inventory; "
        f"missing={missing}, stale={stale}"
    )


def _assert_targets_resolve_within_repository(
    targets: list[str], repository_root: Path = REPO_ROOT
) -> None:
    """Require every navigation target to resolve to an existing in-repository path."""
    root = repository_root.resolve()
    escaped: list[str] = []
    missing: list[str] = []
    for target in targets:
        resolved = (root / target).resolve()
        if not resolved.is_relative_to(root):
            escaped.append(target)
        elif not resolved.exists():
            missing.append(target)

    assert not escaped, f"README repository navigation escapes the repository: {escaped}"
    assert not missing, f"README repository navigation has broken links: {missing}"


def test_navigation_exclusion_policy_is_explicit() -> None:
    """README itself is the only tracked root entry excluded from navigation."""
    assert set(NAVIGATION_EXCLUSIONS) == {"README.md"}
    assert all(reason.strip() for reason in NAVIGATION_EXCLUSIONS.values())


def test_navigation_table_is_exact_tracked_root_inventory() -> None:
    """Each tracked root entry has one direct navigation-table target."""
    tracked = _tracked_top_level_entries()
    exclusions = set(NAVIGATION_EXCLUSIONS)
    assert exclusions <= tracked

    _assert_exact_root_navigation_inventory(_navigation_table_targets())


def test_navigation_table_targets_resolve_inside_repository() -> None:
    """Every local navigation-table target resolves inside the repository."""
    _assert_targets_resolve_within_repository(_navigation_table_targets())


def test_navigation_inventory_rejects_stale_or_nested_table_targets() -> None:
    """A nested or stale table target cannot masquerade as a root entry."""
    expected = _tracked_top_level_entries() - set(NAVIGATION_EXCLUSIONS)

    nested_targets = _navigation_table_targets(
        "\n".join(
            f"| [entry]({target}) | test target |"
            for target in [*sorted(expected - {"hephaestus"}), "hephaestus/automation"]
        )
    )
    with pytest.raises(AssertionError, match="direct top-level targets"):
        _assert_exact_root_navigation_inventory(nested_targets)

    stale_targets = _navigation_table_targets(
        "\n".join(
            f"| [entry]({target}) | test target |"
            for target in [*sorted(expected - {"AGENTS.md"}), "obsolete-root.md"]
        )
    )
    with pytest.raises(AssertionError, match="exact tracked root-path inventory"):
        _assert_exact_root_navigation_inventory(stale_targets)


def test_navigation_target_validation_rejects_repository_escape(tmp_path: Path) -> None:
    """A relative target that resolves above the checkout is rejected."""
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    escaped_target = "../../../outside"
    (tmp_path / "outside").touch()

    with pytest.raises(AssertionError, match="escapes the repository"):
        _assert_targets_resolve_within_repository([escaped_target], repository_root)


def test_navigation_replaces_hand_maintained_directory_tree() -> None:
    """README must not duplicate repository structure as a directory tree."""
    readme = README.read_text(encoding="utf-8")
    offenders = [
        f"{line_number}: {line}"
        for line_number, line in enumerate(readme.splitlines(), start=1)
        if TREE_MARKER_RE.search(line)
    ]
    assert not offenders, f"README contains hand-maintained tree entries: {offenders}"


def test_agents_md_tree_lists_every_subpackage() -> None:
    """Every real subpackage must appear in the authoritative AGENTS.md tree."""
    agents_md = AGENTS_MD.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _real_subpackages()
        if f"├── {name}/" not in agents_md and f"└── {name}/" not in agents_md
    )
    assert not missing, f"AGENTS.md directory tree omits subpackage(s): {missing}"
