"""Tests for scripts/check_license_compatibility.py."""

import importlib.metadata as md
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.io.toml import import_tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_license_compatibility import (
    ALLOWED_EXTRA_COPYLEFT,
    DIST_NAME,
    RUNTIME_EXTRAS,
    _FixtureMeta,
    distributed_requirements,
    is_compatible,
    main,
    resolve_license,
    scan,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestResolveLicenseSynthetic:
    """resolve_license field precedence on synthetic fixtures."""

    def test_prefers_license_expression(self):
        assert resolve_license(_FixtureMeta({"License-Expression": "MIT"})) == ["MIT"]

    def test_or_expression_splits(self):
        m = _FixtureMeta({"License-Expression": "Apache-2.0 OR BSD-2-Clause"})
        assert resolve_license(m) == ["Apache-2.0", "BSD-2-Clause"]

    def test_falls_back_to_trove(self):
        m = _FixtureMeta(
            {
                "Classifier": [
                    "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)"
                ]
            }
        )
        assert resolve_license(m) == ["LGPL-3.0"]

    def test_falls_back_to_freeform_alias(self):
        assert resolve_license(_FixtureMeta({"License": "PSFL"})) == ["PSF-2.0"]


class TestResolveLicenseReal:
    """Pin the ACTUAL installed-metadata schema — catches canonicalization drift."""

    @pytest.mark.parametrize(
        ("pkg", "expected_subset"),
        [
            ("pyyaml", {"MIT"}),
            ("packaging", {"Apache-2.0", "BSD-2-Clause"}),
            ("pydantic", {"MIT"}),
        ],
    )
    def test_known_installed_packages_resolve(self, pkg, expected_subset):
        ids = set(resolve_license(md.metadata(pkg)))
        assert ids & expected_subset, f"{pkg} resolved to {ids}"

    def test_pygithub_resolves_to_lgpl_if_installed(self):
        try:
            ids = resolve_license(md.metadata("pygithub"))
        except md.PackageNotFoundError:
            pytest.skip("pygithub (github extra) not installed in this env")
        assert "LGPL-3.0" in ids or "LGPL" in ids
        assert is_compatible("pygithub", ids)


class TestIsCompatible:
    """is_compatible two-tier allowlist (blanket permissive + per-pkg copyleft)."""

    def test_permissive_allowed_for_any_package(self):
        assert is_compatible("anything", ["MIT"]) is True

    def test_or_with_one_permissive_disjunct_allowed(self):
        assert is_compatible("anything", ["MIT", "GPL-3.0"]) is True

    def test_bare_gpl_rejected(self):
        assert is_compatible("anything", ["GPL-3.0"]) is False

    def test_lgpl_allowed_only_for_pygithub(self):
        assert is_compatible("pygithub", ["LGPL-3.0"]) is True
        assert is_compatible("some-new-dep", ["LGPL-3.0"]) is False

    def test_psf_allowed_only_for_defusedxml(self):
        assert is_compatible("defusedxml", ["PSF-2.0"]) is True
        assert is_compatible("other-pkg", ["PSF-2.0"]) is False

    def test_python_2_0_psf_spelling_allowed_only_for_defusedxml(self):
        # ``Python-2.0`` is the alternate SPDX spelling of PSF; it is scoped
        # per-package to defusedxml, NOT blanket-permissive. Removing it from
        # PERMISSIVE means any other package carrying it must be rejected.
        assert is_compatible("defusedxml", ["Python-2.0"]) is True
        assert is_compatible("other-pkg", ["Python-2.0"]) is False


class TestLoudFailure:
    """The gate must FAIL, never silently pass, when blind."""

    def test_empty_requires_dist_exits_nonzero(self):
        empty = _FixtureMeta({})
        with patch("check_license_compatibility.md.metadata", return_value=empty):
            with pytest.raises(SystemExit) as exc:
                distributed_requirements(None)
        assert exc.value.code == 2

    def test_package_not_installed_exits_nonzero(self):
        # The package itself absent => loud install hint + exit 2, never a
        # traceback or silent pass.
        with patch(
            "check_license_compatibility.md.metadata",
            side_effect=md.PackageNotFoundError(DIST_NAME),
        ):
            with pytest.raises(SystemExit) as exc:
                distributed_requirements(None)
        assert exc.value.code == 2

    def test_uninstalled_installable_dep_exits_nonzero(self):
        # installable_now=True => a genuine coverage hole => loud exit 2.
        with patch(
            "check_license_compatibility.distributed_requirements",
            return_value=[("ghost", True)],
        ):
            with patch(
                "check_license_compatibility.md.metadata",
                side_effect=md.PackageNotFoundError("ghost"),
            ):
                with pytest.raises(SystemExit) as exc:
                    scan(None)
        assert exc.value.code == 2

    def test_uninstalled_other_python_dep_skipped_not_failed(self):
        # installable_now=False (marker excludes this interpreter, e.g. tomli on
        # Python >= 3.11) => correctly absent => skipped, not a coverage hole.
        with patch(
            "check_license_compatibility.distributed_requirements",
            return_value=[("tomli", False)],
        ):
            with patch(
                "check_license_compatibility.md.metadata",
                side_effect=md.PackageNotFoundError("tomli"),
            ):
                assert scan(None) == []


class TestDistributedScope:
    """distributed_requirements selects the distributed set, excludes dev."""

    def _dist_or_skip(self):
        # Resolving the distributed set reads the installed package's
        # Requires-Dist and returns (name, installable_now) tuples. CI installs
        # `.[all]`; a bare local env may not have the package installed — skip
        # rather than mask, the CI gate is authoritative.
        try:
            return {name for name, _ in distributed_requirements(None)}
        except (md.PackageNotFoundError, SystemExit):
            pytest.skip("HomericIntelligence-Hephaestus not installed in this env")
            return set()  # unreachable (pytest.skip raises); makes the set return type explicit

    def test_dev_tools_never_examined(self):
        dist = self._dist_or_skip()
        assert "ruff" not in dist and "yamllint" not in dist and "pytest" not in dist

    def test_runtime_extras_excludes_dev(self):
        assert "dev" not in RUNTIME_EXTRAS

    def test_platform_gated_dep_included(self):
        # tzdata is gated platform_system == 'Windows'; must still be in scope.
        assert "tzdata" in self._dist_or_skip()

    def test_clean_distributed_tree_passes_when_all_installed(self):
        # If the package or an extra (e.g. nats-py) is missing locally, scan()
        # raises PackageNotFoundError / SystemExit(2); this test surfaces that as
        # a skip rather than masking it. CI installs `.[all]` so it runs there.
        try:
            assert scan() == []
        except md.PackageNotFoundError:
            pytest.skip("HomericIntelligence-Hephaestus not installed in this env")
        except SystemExit as e:
            pytest.skip(f"runtime extra not installed locally: {e}")


class TestAllExtraCompleteness:
    """Coverage depends on `[all]` aggregating every runtime extra (Finding 3)."""

    def test_all_extra_covers_runtime_extras(self):
        # Parse pyproject.toml properly rather than regex-scraping it: a hand-rolled
        # pattern breaks the moment `[all]` is reformatted (multi-line array, a
        # comment, or the dist-name placement shifts). tomllib gives the structured
        # value; we then pull the extras out of the single aggregate requirement,
        # e.g. "HomericIntelligence-Hephaestus[automation,github,nats,toml,xml,schema]".
        tomllib = import_tomllib()
        if tomllib is None:
            pytest.skip("tomllib/tomli not available to parse pyproject.toml")
            return  # unreachable (pytest.skip raises); narrows tomllib for mypy
        with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
            pyproject = tomllib.load(fh)
        all_specs = pyproject["project"]["optional-dependencies"]["all"]
        m = re.search(r"\[([^\]]+)\]", " ".join(all_specs))
        assert m, (
            "could not find the bracketed extras aggregate in the `all` extra "
            f"{all_specs!r} of pyproject.toml"
        )
        aggregated = {e.strip() for e in m.group(1).split(",")}
        # Every runtime extra except the self-referential `all` must be aggregated.
        for extra in RUNTIME_EXTRAS - {"all"}:
            assert extra in aggregated, (
                f"runtime extra {extra!r} is in RUNTIME_EXTRAS but missing from "
                f"pyproject `[all]` aggregate {sorted(aggregated)} — its deps would "
                "never install and the license gate would skip them."
            )


class TestMain:
    """main() exit-code contract: blocking on PR, advisory on main."""

    def _fixture(self, tmp_path, records):
        p = tmp_path / "meta.json"
        p.write_text(json.dumps(records))
        return str(p)

    def test_violation_on_pr_returns_one(self, tmp_path, monkeypatch):
        path = self._fixture(tmp_path, {"evil": {"License-Expression": "GPL-3.0"}})
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        with patch.object(sys, "argv", ["x", "--metadata-json", path]):
            assert main() == 1

    def test_violation_on_main_is_advisory_zero(self, tmp_path, monkeypatch):
        path = self._fixture(tmp_path, {"evil": {"License-Expression": "GPL-3.0"}})
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        with patch.object(sys, "argv", ["x", "--metadata-json", path]):
            assert main() == 0

    def test_pygithub_lgpl_fixture_passes(self, tmp_path):
        path = self._fixture(
            tmp_path,
            {
                "pygithub": {
                    "Classifier": [
                        "License :: OSI Approved :: GNU Library or "
                        "Lesser General Public License (LGPL)"
                    ]
                }
            },
        )
        with patch.object(sys, "argv", ["x", "--metadata-json", path]):
            assert main() == 0

    def test_allowlist_covers_notice_extras(self):
        assert "pygithub" in ALLOWED_EXTRA_COPYLEFT
        assert "defusedxml" in ALLOWED_EXTRA_COPYLEFT
