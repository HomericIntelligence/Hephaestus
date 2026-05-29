"""Tests for hephaestus.config.pixi shared dependency-section detector.

Pins both consumers' behaviour so the DRY consolidation stays
behaviour-preserving:

- ``include_pypi=True`` reproduces ``config.dep_sync._is_deps_section``.
- ``include_pypi=False`` reproduces ``ci.precommit._is_deps_section_header``.
"""

from __future__ import annotations

from hephaestus.config.pixi import is_deps_section


class TestIncludePypi:
    """``include_pypi=True`` — dep_sync semantics (accepts pypi-dependencies)."""

    def test_dependencies(self) -> None:
        assert is_deps_section("[dependencies]", include_pypi=True) is True

    def test_pypi_dependencies(self) -> None:
        assert is_deps_section("[pypi-dependencies]", include_pypi=True) is True

    def test_feature_dependencies(self) -> None:
        assert is_deps_section("[feature.dev.dependencies]", include_pypi=True) is True

    def test_feature_pypi_dependencies(self) -> None:
        assert is_deps_section("[feature.dev.pypi-dependencies]", include_pypi=True) is True

    def test_feature_hyphenated_name(self) -> None:
        assert is_deps_section("[feature.test-tools.dependencies]", include_pypi=True) is True

    def test_project_rejected(self) -> None:
        assert is_deps_section("[project]", include_pypi=True) is False

    def test_bare_feature_rejected(self) -> None:
        assert is_deps_section("[feature.dev]", include_pypi=True) is False

    def test_tasks_rejected(self) -> None:
        assert is_deps_section("[tasks]", include_pypi=True) is False

    def test_surrounding_whitespace_tolerated(self) -> None:
        assert is_deps_section("  [dependencies]  ", include_pypi=True) is True

    def test_inline_comment_tolerated(self) -> None:
        assert is_deps_section("[dependencies] # conda deps", include_pypi=True) is True

    def test_nested_feature_table_rejected(self) -> None:
        # Four-part header is not feature.<name>.dependencies
        assert is_deps_section("[feature.a.b.dependencies]", include_pypi=True) is False


class TestExcludePypi:
    """``include_pypi=False`` — precommit semantics (conda dependencies only)."""

    def test_dependencies(self) -> None:
        assert is_deps_section("[dependencies]", include_pypi=False) is True

    def test_feature_dependencies(self) -> None:
        assert is_deps_section("[feature.dev.dependencies]", include_pypi=False) is True

    def test_pypi_dependencies_rejected(self) -> None:
        assert is_deps_section("[pypi-dependencies]", include_pypi=False) is False

    def test_feature_pypi_dependencies_rejected(self) -> None:
        assert is_deps_section("[feature.dev.pypi-dependencies]", include_pypi=False) is False

    def test_project_rejected(self) -> None:
        assert is_deps_section("[project]", include_pypi=False) is False


class TestDelegationParity:
    """The live wrappers must equal the shared helper for matching options."""

    HEADERS = (
        "[dependencies]",
        "[pypi-dependencies]",
        "[feature.dev.dependencies]",
        "[feature.dev.pypi-dependencies]",
        "[feature.test-tools.dependencies]",
        "[project]",
        "[feature.dev]",
        "[tasks]",
    )

    def test_dep_sync_delegates(self) -> None:
        from hephaestus.config.dep_sync import _is_deps_section

        for h in self.HEADERS:
            assert _is_deps_section(h) == is_deps_section(h, include_pypi=True)

    def test_precommit_delegates(self) -> None:
        from hephaestus.ci.precommit import _is_deps_section_header

        # precommit's caller pre-strips the line; mirror that here.
        for h in self.HEADERS:
            stripped = h.strip()
            assert _is_deps_section_header(stripped) == is_deps_section(
                stripped, include_pypi=False
            )
