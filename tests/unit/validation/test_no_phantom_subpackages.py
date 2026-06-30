"""Guard: no phantom (empty/non-package) directories under hephaestus/.

Regression guard for issue #1459: hephaestus/git/ lingered as a directory
containing only a stale __pycache__/ after its source was removed in #357,
confusing newcomers and polluting the package namespace (YAGNI, POLA).

A directory directly under hephaestus/ is a real subpackage only if it holds
an ``__init__.py``. Any other non-dunder directory (excluding the transient,
gitignored ``__pycache__``) is dead scaffolding and must not be reintroduced.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "hephaestus"


def _phantom_subdirs() -> set[str]:
    """Return non-package directories directly under hephaestus/.

    Skips dunder dirs (``__pycache__`` etc.) and dotfiles — neither is a
    candidate source subpackage. A remaining directory without an
    ``__init__.py`` is a phantom.
    """
    return {
        p.name
        for p in PACKAGE_DIR.iterdir()
        if p.is_dir()
        and not p.name.startswith("__")
        and not p.name.startswith(".")
        and not (p / "__init__.py").exists()
    }


def test_no_phantom_subpackage_directories() -> None:
    """Every hephaestus/ subdirectory must be an importable package."""
    phantoms = sorted(_phantom_subdirs())
    assert not phantoms, (
        "Phantom (non-package) directories found under hephaestus/ "
        f"(no __init__.py): {phantoms}. Delete them or add an __init__.py "
        "if a real subpackage is intended (issue #1459)."
    )
