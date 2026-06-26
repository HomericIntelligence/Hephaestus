"""Regression guard: deprecated config symbols must be annotated in the docs.

Property: a public config symbol that emits a ``DeprecationWarning`` when called
MUST be annotated ``(deprecated)`` in COMPATIBILITY.md's ``hephaestus.config``
table AND listed in docs/MIGRATION.md's "Deprecated symbols" section.

Guards issue #1508: ``get_config_value`` was deprecated in code (utils.py:329-335)
but undocumented in COMPATIBILITY.md and MIGRATION.md.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from hephaestus.config.utils import get_config_value

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPATIBILITY = REPO_ROOT / "COMPATIBILITY.md"
MIGRATION = REPO_ROOT / "docs" / "MIGRATION.md"


def test_get_config_value_emits_deprecation_warning() -> None:
    """Source-of-truth: the symbol really does emit a DeprecationWarning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        get_config_value("nonexistent.key", default=None)
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), (
        "get_config_value must emit a DeprecationWarning"
    )


def test_compatibility_md_annotates_get_config_value_deprecated() -> None:
    """COMPATIBILITY.md table must flag get_config_value as (deprecated)."""
    text = COMPATIBILITY.read_text(encoding="utf-8")
    # Find the hephaestus.config section
    marker = "### `hephaestus.config`"
    assert marker in text, "COMPATIBILITY.md must have a 'hephaestus.config' section"
    section = text.split(marker, 1)[1]
    # Extract just the table (stop at the next section heading)
    for stop in ("### ", "## "):
        if stop in section:
            section = section.split(stop, 1)[0]
    # Verify inline annotation in table row
    assert "get_config_value" in section and "deprecated" in section.lower(), (
        "COMPATIBILITY.md 'hephaestus.config' table must have a row with "
        "get_config_value and (deprecated)"
    )


def test_compatibility_md_deprecated_symbols_callout_has_get_config_value() -> None:
    """COMPATIBILITY.md 'Deprecated symbols' callout must list get_config_value."""
    text = COMPATIBILITY.read_text(encoding="utf-8")
    marker = "**Deprecated symbols**"
    assert marker in text, "COMPATIBILITY.md must have a 'Deprecated symbols' callout"
    callout = text.split(marker, 1)[1]
    # Extract just the callout (stop at the next section or list marker at column 0)
    for stop in ("### ", "## ", "\n| "):
        if stop in callout:
            callout = callout.split(stop, 1)[0]
    assert "get_config_value" in callout, (
        "COMPATIBILITY.md 'Deprecated symbols' callout must list get_config_value"
    )


def test_migration_md_lists_get_config_value_as_deprecated() -> None:
    """MIGRATION.md's 'Deprecated symbols' section must list get_config_value."""
    text = MIGRATION.read_text(encoding="utf-8")
    marker = "### Deprecated symbols"
    assert marker in text, "MIGRATION.md must have a 'Deprecated symbols' section"
    section = text.split(marker, 1)[1]
    for stop in ("\n## ", "\n### "):
        section = section.split(stop, 1)[0]
    assert "get_config_value" in section, (
        "MIGRATION.md 'Deprecated symbols' section must list get_config_value"
    )
