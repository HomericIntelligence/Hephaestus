"""Shared helpers for the line-by-line ``pixi.toml`` dependency-section parser.

Both :mod:`hephaestus.config.dep_sync` and :mod:`hephaestus.ci.precommit` walk
``pixi.toml`` line by line (to avoid requiring a TOML parser on Python 3.10) and
need to recognise dependency section headers. They differ in one respect:
``dep_sync`` also treats ``pypi-dependencies`` sections as dependency sections,
whereas ``precommit`` only cares about conda ``dependencies``. That single
difference is expressed via the ``include_pypi`` flag.

Recognised patterns (with ``include_pypi=True``):

- ``[dependencies]``
- ``[pypi-dependencies]``
- ``[feature.<name>.dependencies]``
- ``[feature.<name>.pypi-dependencies]``

With ``include_pypi=False`` only the two ``dependencies`` forms match.
"""

from __future__ import annotations


def is_deps_section(header: str, *, include_pypi: bool = True) -> bool:
    """Return True if *header* is a pixi.toml dependency section header.

    Args:
        header: A TOML section header line including the brackets. Surrounding
            whitespace and a trailing inline comment are tolerated.
        include_pypi: If True (default), ``pypi-dependencies`` sections count as
            dependency sections. If False, only conda ``dependencies`` match.

    Returns:
        True if the section holds package→version entries we care about.

    """
    # Strip brackets and optional inline comment.
    inner = header.strip().lstrip("[").split("]")[0].split("#")[0].strip()
    section_names = ("dependencies", "pypi-dependencies") if include_pypi else ("dependencies",)
    if inner in section_names:
        return True
    # feature.<name>.dependencies (and pypi-dependencies when include_pypi).
    parts = inner.split(".")
    return len(parts) == 3 and parts[0] == "feature" and parts[2] in section_names
