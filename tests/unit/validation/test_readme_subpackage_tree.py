"""Guards for README root navigation and the AGENTS.md package inventory."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "hephaestus"
README = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

NAVIGATION_HEADING = "## Repository navigation"
NAVIGATION_EXCLUSIONS = {
    "README.md": "The navigation index lives in README.md, so a self-link adds no value.",
}
LINK_TARGET_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
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


def _local_navigation_targets() -> list[str]:
    """Return normalized local link targets from the navigation section."""
    targets: list[str] = []
    for raw_target in LINK_TARGET_RE.findall(_navigation_section()):
        target = raw_target.strip().strip("<>").split("#", 1)[0].rstrip("/")
        if target and not target.startswith("/") and "://" not in target:
            targets.append(target.removeprefix("./"))
    return targets


def test_navigation_exclusion_policy_is_explicit() -> None:
    """README itself is the only tracked root entry excluded from navigation."""
    assert set(NAVIGATION_EXCLUSIONS) == {"README.md"}
    assert all(reason.strip() for reason in NAVIGATION_EXCLUSIONS.values())


def test_navigation_covers_every_tracked_top_level_entry() -> None:
    """Every tracked root file or directory is linked regardless of extension."""
    tracked = _tracked_top_level_entries()
    exclusions = set(NAVIGATION_EXCLUSIONS)
    assert exclusions <= tracked

    linked = {target.split("/", 1)[0] for target in _local_navigation_targets()}
    missing = sorted(tracked - exclusions - linked)
    assert not missing, f"README repository navigation omits tracked root entries: {missing}"


def test_navigation_links_resolve() -> None:
    """Every local target in the repository-navigation section exists."""
    missing = sorted(
        target for target in _local_navigation_targets() if not (REPO_ROOT / target).exists()
    )
    assert not missing, f"README repository navigation has broken links: {missing}"


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
