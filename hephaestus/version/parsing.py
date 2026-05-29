"""Shared version-string parsing core.

Several modules parsed dotted version strings into comparable integer tuples,
each with subtly different handling of non-numeric segments:

- ``config.dep_sync._parse_version`` splits on ``.`` and ``-`` and **drops** any
  segment that is not all-digits (e.g. ``"1.2.3rc1"`` → ``(1, 2)``).
- ``ci.precommit._version_tuple`` splits on ``.`` and **coerces** any
  non-integer segment to ``0`` (e.g. ``"1.2.rc1"`` → ``(1, 2, 0)``).
- ``version.consistency._parse_version_tuple`` splits on ``.`` and **raises** on
  any non-integer segment (it is only ever fed pre-validated ``X.Y.Z`` strings).

This module exposes one configurable core, :func:`parse_version_tuple`, plus
thin convenience wrappers that reproduce each call site's exact behaviour. The
strict 3-component semver validator lives in
:func:`hephaestus.version.manager.parse_version`, which delegates its integer
conversion here after its own regex validation.
"""

from __future__ import annotations

import re
from typing import Literal

#: Strategy for handling a segment that is not a base-10 integer.
#:
#: - ``"drop"``: silently skip the segment.
#: - ``"zero"``: replace it with ``0``.
#: - ``"raise"``: let ``int()`` raise :class:`ValueError`.
NonNumeric = Literal["drop", "zero", "raise"]


def parse_version_tuple(
    version: str,
    *,
    split_pattern: str = r"\.",
    on_non_numeric: NonNumeric = "raise",
) -> tuple[int, ...]:
    r"""Parse a dotted version string into a tuple of ints.

    Args:
        version: Version string such as ``"1.2.3"``.
        split_pattern: Regex used to split the string into segments. Defaults to
            a literal dot; pass ``r"[.\\-]"`` to also split on dashes.
        on_non_numeric: How to handle a segment that is not a base-10 integer:
            ``"drop"`` skips it, ``"zero"`` substitutes ``0``, and ``"raise"``
            propagates the :class:`ValueError` from ``int()``.

    Returns:
        Tuple of integers, e.g. ``(1, 2, 3)``.

    Raises:
        ValueError: If ``on_non_numeric`` is ``"raise"`` and a segment is not a
            valid integer.

    """
    parts: list[int] = []
    for segment in re.split(split_pattern, version):
        if on_non_numeric == "drop":
            if segment.isdigit():
                parts.append(int(segment))
            continue
        try:
            parts.append(int(segment))
        except ValueError:
            if on_non_numeric == "zero":
                parts.append(0)
            else:  # "raise"
                raise
    return tuple(parts)
