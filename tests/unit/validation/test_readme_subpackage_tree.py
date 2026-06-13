"""Guard: README directory tree must list every hephaestus/ subpackage.

Prevents doc-vs-reality drift (issue #1188): scripts_lib/ was on disk but
absent from the README tree while the doc still claimed 19 subpackages.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "hephaestus"
README = REPO_ROOT / "README.md"


def _real_subpackages() -> set[str]:
    """Return the names of every importable hephaestus/ subpackage on disk."""
    return {
        p.name
        for p in PACKAGE_DIR.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith("__")
    }


def test_readme_tree_lists_every_subpackage() -> None:
    """Every real subpackage must appear in the README directory tree block."""
    readme = README.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _real_subpackages()
        if f"├── {name}/" not in readme and f"└── {name}/" not in readme
    )
    assert not missing, f"README directory tree omits subpackage(s): {missing}"
