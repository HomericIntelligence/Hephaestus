"""Test current GitHub metadata helper functions."""

from __future__ import annotations

import pytest


class TestGithubApiPureFunctions:
    """Test pure helpers in github_api."""

    def test_parse_issue_number_from_url(self) -> None:
        # Regex: r"/issues/(\d+)" — github_api.py:419
        from hephaestus.automation.github_api import _parse_issue_number

        assert _parse_issue_number("https://github.com/org/repo/issues/42") == 42

    def test_parse_issue_number_bare_numeric_string(self) -> None:
        # Fallback: int(output.split("/")[-1]) — github_api.py:422
        from hephaestus.automation.github_api import _parse_issue_number

        assert _parse_issue_number("99") == 99

    def test_assert_body_has_closes_valid_line(self) -> None:
        from hephaestus.automation.github_api import _assert_body_has_closes

        # Must not raise
        _assert_body_has_closes("Summary\n\nCloses #42\n")

    def test_assert_body_has_closes_missing_raises_value_error(self) -> None:
        # Raises ValueError — confirmed github_api.py:541
        from hephaestus.automation.github_api import _assert_body_has_closes

        with pytest.raises(ValueError):
            _assert_body_has_closes("Summary\n\nFixes #42\n")

    def test_assert_body_has_closes_fixes_keyword_not_accepted(self) -> None:
        from hephaestus.automation.github_api import _assert_body_has_closes

        with pytest.raises(ValueError):
            _assert_body_has_closes("Fixes #42")

    def test_parse_issue_dependencies_finds_deps(self) -> None:
        from hephaestus.automation.github_api import parse_issue_dependencies

        deps = parse_issue_dependencies("Depends on #10, #20")
        assert 10 in deps
        assert 20 in deps

    def test_parse_issue_dependencies_no_deps(self) -> None:
        from hephaestus.automation.github_api import parse_issue_dependencies

        assert parse_issue_dependencies("No dependencies here") == []
