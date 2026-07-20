"""Guard: README and AGENTS.md directory trees must list every hephaestus/ subpackage.

Prevents doc-vs-reality drift (issues #1188, #1449): scripts_lib/ was on disk
but absent from the agent-contract tree while the doc still claimed 20 subpackages.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "hephaestus"
README = REPO_ROOT / "README.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"


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


def test_agents_md_tree_lists_every_subpackage() -> None:
    """Every real subpackage must appear in the AGENTS.md directory tree block."""
    agents_md = AGENTS_MD.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in _real_subpackages()
        if f"├── {name}/" not in agents_md and f"└── {name}/" not in agents_md
    )
    assert not missing, f"AGENTS.md directory tree omits subpackage(s): {missing}"
