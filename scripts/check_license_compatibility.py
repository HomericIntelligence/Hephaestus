#!/usr/bin/env python3
"""Fail CI when a *distributed* dependency's license is incompatible with BSD-3-Clause.

NOTICE holds the authoritative human-readable analysis. This script is the
machine-enforced subset: every dependency the project DISTRIBUTES (base deps +
runtime extras, NEVER `dev`) must carry a permissive license or be a
NOTICE-justified non-vendored extra (pygithub/LGPL, defusedxml/PSF).

Scope = distributed deps only, read from the installed package's Requires-Dist.
Dev tooling (yamllint/bats are GPL, see NOTICE) is never examined.

FAILS LOUDLY (never silently passes):
  * empty/missing Requires-Dist -> the gate cannot see its inputs -> exit 2
  * a declared distributed dep with no resolvable metadata (extra not installed)
    -> coverage hole -> exit 2. CI installs `.[all]` on a setup-python runner first.
  * a license string not in the mapping tables -> exit (PR:1 / main:0) with a
    hint to extend TROVE_TO_SPDX / LICENSE_ALIASES.
  * a distributed dep whose PEP 508 marker excludes the current interpreter
    -> classified via FALLBACK_LICENSES (NOTICE-backed); if no entry exists
    -> exit 2. Add new marker-excluded deps to FALLBACK_LICENSES.

Stdlib only (importlib.metadata + packaging, both already runtime deps); runs
under plain `python3` once the package + extras are pip-installed.

Advisory on main/schedule (exit 0), blocking on pull_request (exit 1).

Usage:
    python3 scripts/check_license_compatibility.py
    python3 scripts/check_license_compatibility.py --metadata-json fixtures.json
"""

import argparse
import importlib.metadata as md
import json
import os
import sys

from packaging.requirements import InvalidRequirement, Requirement

DIST_NAME = "HomericIntelligence-Hephaestus"

# Extras whose packages are DISTRIBUTED. `dev` is deliberately absent.
RUNTIME_EXTRAS: frozenset[str] = frozenset(
    {"all", "automation", "github", "nats", "toml", "xml", "schema"}
)

PERMISSIVE: frozenset[str] = frozenset({"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC"})

# Per-package, NOTICE-justified non-vendored copyleft/non-permissive licenses.
# PSF (both SPDX spellings ``PSF-2.0`` and ``Python-2.0``) is scoped here, NOT in
# PERMISSIVE: NOTICE allows PSF only for defusedxml, so it must be rejected for
# any other package (see test_psf_allowed_only_for_defusedxml).
ALLOWED_EXTRA_COPYLEFT: dict[str, frozenset[str]] = {
    "pygithub": frozenset({"LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later", "LGPL"}),
    "defusedxml": frozenset({"PSF-2.0", "Python-2.0"}),
}

# Static license fallback for distributed deps whose PEP 508 marker excludes
# the CI interpreter/platform (tomli: python_version < '3.11'; tzdata:
# platform_system == 'Windows'). These packages are never installed in the
# current CI leg so their metadata is not resolvable; licenses are taken from
# NOTICE, which is the human-maintained authoritative record. Any future
# incompatible license change MUST update both NOTICE and this map.
FALLBACK_LICENSES: dict[str, list[str]] = {
    "tomli": ["MIT"],  # NOTICE: "toml extra / tomli  MIT"
    "tzdata": ["Apache-2.0"],  # NOTICE: "tzdata  Apache-2.0"
}

TROVE_TO_SPDX: dict[str, str] = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL-3.0",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Lesser General Public License v3 or later (LGPLv3+)": "LGPL-3.0",
    "GNU General Public License (GPL)": "GPL-3.0",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
}

LICENSE_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "isc": "ISC",
    "psfl": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
}

# Marker matrix: any-satisfiable => the dep is distributed somewhere, so its
# license must be checked. Spans Python floor..current AND platforms, so a
# platform-gated dep (e.g. tzdata on Windows) is NOT silently dropped on Linux CI.
_PY = ("3.10", "3.13")
_PLAT = (
    {"sys_platform": "linux", "platform_system": "Linux"},
    {"sys_platform": "win32", "platform_system": "Windows"},
    {"sys_platform": "darwin", "platform_system": "Darwin"},
)


def _marker_ok(req: Requirement, extra: str) -> bool:
    """True if req.marker can hold for `extra` under ANY supported Python/platform."""
    if req.marker is None:
        return True
    for pyver in _PY:
        for plat in _PLAT:
            if req.marker.evaluate({"extra": extra, "python_version": pyver, **plat}):
                return True
    return False


def _installable_in_current_env(req: Requirement) -> bool:
    """True if req.marker holds for the CURRENT interpreter/platform (some extra).

    A dep selected by the cross-version scope matrix may still be correctly
    absent in this interpreter — e.g. ``tomli; python_version < '3.11'`` is not
    installed on Python 3.13. We only demand metadata for deps that THIS env
    would actually install; others are classified on a different CI Python row.
    """
    if req.marker is None:
        return True
    return any(req.marker.evaluate({"extra": e}) for e in ("", *RUNTIME_EXTRAS))


def distributed_requirements(records: dict[str, dict] | None) -> list[tuple[str, bool]]:
    """Return [(package_name, installable_now)] for every DISTRIBUTED dependency.

    Selects base deps + runtime-extra deps (NEVER ``dev``) from the installed
    package's ``Requires-Dist``. ``installable_now`` is True when the dep's
    marker holds for the current interpreter/platform, so callers can distinguish
    a genuine coverage hole (must classify) from a correctly-absent
    other-Python-only dep (skip).

    Args:
        records: Optional fixture mapping (package name -> metadata dict). When
            provided, its keys ARE the distributed set (always installable_now).

    Returns:
        Sorted list of ``(package_name, installable_now)`` tuples (lowercased).

    Raises:
        SystemExit: code 2 if the package metadata exposes no Requires-Dist — a
            gate that cannot see its inputs must fail loudly, never pass silently.

    """
    if records is not None:
        return [(name, True) for name in sorted(records)]
    try:
        meta = md.metadata(DIST_NAME)
    except md.PackageNotFoundError:
        print(
            f"FATAL: {DIST_NAME!r} is not installed; cannot read its dependency "
            "set. Install it first: `pip install -e .[all]` (CI does this in the "
            "license-scan job before invoking this script).",
            file=sys.stderr,
        )
        sys.exit(2)
    reqs = meta.get_all("Requires-Dist") or []
    if not reqs:
        print(
            f"FATAL: no Requires-Dist for {DIST_NAME!r}; package metadata missing. "
            "Install the package (pip install -e .[all]) before scanning.",
            file=sys.stderr,
        )
        sys.exit(2)
    selected: dict[str, bool] = {}
    for raw in reqs:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if req.marker is None:
            selected[req.name.lower()] = True  # base dep, always distributed
            continue
        if not any(_marker_ok(req, e) for e in ("", *RUNTIME_EXTRAS)):
            continue
        # In scope. A package may appear under several markers; it is
        # installable-now if ANY of its specs holds for the current env.
        name = req.name.lower()
        selected[name] = selected.get(name, False) or _installable_in_current_env(req)
    return sorted(selected.items())


def _trove_ids(meta) -> list[str]:
    """Return SPDX ids mapped from a package's trove License classifiers."""
    out = []
    for c in meta.get_all("Classifier") or []:
        if c.startswith("License ::"):
            tail = c.split("::")[-1].strip()
            if tail in TROVE_TO_SPDX:
                out.append(TROVE_TO_SPDX[tail])
    return out


def resolve_license(meta) -> list[str]:
    """Canonical license id(s), most-standardized field first.

    Reads ``License-Expression`` (SPDX, may be ``A OR B``) -> trove
    ``Classifier`` -> freeform ``License``. Returns a list so an SPDX
    ``A OR B`` expression yields each disjunct.
    """
    expr = meta.get("License-Expression")
    if expr:
        return [p.strip() for p in expr.replace(" or ", " OR ").split(" OR ")]
    trove = _trove_ids(meta)
    if trove:
        return trove
    raw = (meta.get("License") or "").strip()
    if raw:
        first = raw.splitlines()[0].strip()
        return [LICENSE_ALIASES.get(first.lower(), first)]
    return ["UNKNOWN"]


def is_compatible(pkg: str, ids: list[str]) -> bool:
    """True if `pkg` with resolved license `ids` may be distributed."""
    if any(i in PERMISSIVE for i in ids):  # OR-expr: any permissive disjunct wins
        return True
    allowed = ALLOWED_EXTRA_COPYLEFT.get(pkg.lower(), frozenset())
    return any(i in allowed for i in ids)


class _FixtureMeta:
    """Minimal importlib.metadata.Message stand-in for tests/CI fixtures."""

    def __init__(self, rec: dict) -> None:
        self._rec = rec

    def get(self, key: str):
        """Return a single metadata field value (or None)."""
        return self._rec.get(key)

    def get_all(self, key: str):
        """Return a list of metadata field values (or None), like email.Message."""
        val = self._rec.get(key)
        if val is None:
            return None
        return val if isinstance(val, list) else [val]


def _meta_for(name: str, records: dict[str, dict] | None):
    """Return metadata for `name`, from fixtures if provided else installed pkg."""
    if records is not None:
        return _FixtureMeta(records[name])
    return md.metadata(name)  # raises PackageNotFoundError if uninstalled


def scan(records: dict[str, dict] | None = None) -> list[tuple[str, list[str]]]:
    """Return [(package, ids)] for every distributed dep that is NOT compatible.

    Raises:
        SystemExit: code 2 if a DECLARED distributed dep that THIS env would
            install has no metadata (an extra was not installed) — a coverage
            hole, not a pass. CI installs .[all] first. Deps absent only because
            their marker excludes the current interpreter are classified from
            FALLBACK_LICENSES; if FALLBACK_LICENSES has no entry for such a dep
            the gate exits 2 (fail-closed, requires map update).

    """
    violations: list[tuple[str, list[str]]] = []
    for pkg, installable_now in distributed_requirements(records):
        try:
            ids = resolve_license(_meta_for(pkg, records))
        except md.PackageNotFoundError:
            if installable_now:
                print(
                    f"FATAL: distributed dependency {pkg!r} is declared and "
                    "installable in this environment but not installed; cannot "
                    "classify its license. CI must `pip install -e .[all]` first.",
                    file=sys.stderr,
                )
                sys.exit(2)
            fallback = FALLBACK_LICENSES.get(pkg)
            if fallback is None:
                print(
                    f"FATAL: distributed dependency {pkg!r} is not installable "
                    "in this environment (marker excludes it) AND has no entry in "
                    "FALLBACK_LICENSES. Add its NOTICE-documented license to "
                    "FALLBACK_LICENSES in scripts/check_license_compatibility.py.",
                    file=sys.stderr,
                )
                sys.exit(2)
            print(
                f"note: {pkg!r} not installable here (marker excluded); "
                f"classifying from FALLBACK_LICENSES: {fallback}",
                file=sys.stderr,
            )
            ids = fallback
        if not is_compatible(pkg, ids):
            violations.append((pkg, ids))
    return violations


def main() -> int:
    """Scan distributed-dependency licenses; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metadata-json",
        help="JSON {pkg: {License-Expression|License|Classifier:...}} fixture for tests/CI.",
    )
    args = parser.parse_args()

    records = None
    if args.metadata_json:
        with open(args.metadata_json) as fh:
            records = json.load(fh)

    violations = scan(records)

    if not violations:
        print("License scan OK: all distributed dependencies compatible with BSD-3-Clause.")
        return 0

    print("ERROR: distributed dependencies with NOTICE-incompatible licenses:")
    for pkg, ids in sorted(violations):
        print(f"  {pkg}: {', '.join(ids)}")
    print(
        "\nIf a license string is unrecognized, extend TROVE_TO_SPDX / LICENSE_ALIASES. "
        "If this is an intentional non-vendored optional extra, add it to "
        "ALLOWED_EXTRA_COPYLEFT AND document it in NOTICE."
    )

    if os.environ.get("GITHUB_EVENT_NAME", "") == "pull_request":
        return 1
    print("(advisory: non-PR event, exit 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
