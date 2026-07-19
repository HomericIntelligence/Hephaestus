"""Validate CONTRIBUTING.md onboarding commands against the real toolchain.

Regression guard for issue #2166: onboarding once instructed
``pixi shell -e dev`` while ``pixi.toml`` defined no ``dev`` environment.
The uv-only migration (ADR-0008, PR #2236) removed Pixi entirely; these
tests pin that invariant so stale environment-manager instructions and
references to nonexistent toolchain targets cannot return to the
onboarding surfaces.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ONBOARDING_DOCS = (REPO_ROOT / "CONTRIBUTING.md", REPO_ROOT / "README.md")
JUSTFILE = REPO_ROOT / "justfile"

# Recipe headers sit at column 0 as `name:` or `name arg1:`. Variable
# assignments (`var := ...`) never match: their colon is followed by `=`.
_RECIPE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)(?:\s+[^:=\n]*)?:(?!=)", re.MULTILINE)
_JUST_REF_RE = re.compile(r"`just\s+([A-Za-z][A-Za-z0-9_-]*)`")


def _justfile_recipes() -> set[str]:
    return set(_RECIPE_RE.findall(JUSTFILE.read_text(encoding="utf-8")))


def test_onboarding_docs_do_not_reference_pixi() -> None:
    """Onboarding may mention only uv as its environment manager (ADR-0008)."""
    for doc in ONBOARDING_DOCS:
        assert "pixi" not in doc.read_text(encoding="utf-8").lower(), (
            f"{doc.name} references Pixi; the development workflow is uv-only"
        )


def test_no_tracked_pixi_manifests() -> None:
    """A reintroduced pixi.toml/pixi.lock would resurrect the dual-manager split."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    offenders = [p for p in tracked if Path(p).name in {"pixi.toml", "pixi.lock"}]
    assert not offenders, f"tracked Pixi manifests found: {offenders}"


def test_documented_just_recipes_exist() -> None:
    """Every `just <recipe>` in onboarding docs must be a real justfile recipe."""
    recipes = _justfile_recipes()
    assert recipes, "failed to parse any recipes from justfile"
    for doc in ONBOARDING_DOCS:
        referenced = set(_JUST_REF_RE.findall(doc.read_text(encoding="utf-8")))
        missing = referenced - recipes
        assert not missing, f"{doc.name} references nonexistent just recipes: {sorted(missing)}"
