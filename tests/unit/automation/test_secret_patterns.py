"""Tests for hephaestus.automation._secret_patterns."""

from __future__ import annotations

import pytest

from hephaestus.automation._secret_patterns import (
    SECRET_FILE_EXTENSIONS,
    SECRET_FILE_NAMES,
)


class TestSecretFileNames:
    """Tests for SECRET_FILE_NAMES."""

    def test_is_frozenset(self) -> None:
        assert isinstance(SECRET_FILE_NAMES, frozenset)

    @pytest.mark.parametrize(
        "name",
        [
            ".env",
            ".secret",
            "credentials.json",
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
        ],
    )
    def test_contains_expected_names(self, name: str) -> None:
        assert name in SECRET_FILE_NAMES, f"Expected {name!r} in SECRET_FILE_NAMES"

    def test_immutable(self) -> None:
        with pytest.raises(AttributeError):
            SECRET_FILE_NAMES.add("should_fail")  # type: ignore[attr-defined]


class TestSecretFileExtensions:
    """Tests for SECRET_FILE_EXTENSIONS."""

    def test_is_frozenset(self) -> None:
        assert isinstance(SECRET_FILE_EXTENSIONS, frozenset)

    @pytest.mark.parametrize(
        "ext",
        [
            ".env",
            ".pem",
            ".key",
            ".pfx",
            ".p12",
        ],
    )
    def test_contains_expected_extensions(self, ext: str) -> None:
        # .env is in SECRET_FILE_NAMES; the others are in SECRET_FILE_EXTENSIONS.
        # Test that at least one of the two sets contains each expected pattern.
        assert ext in SECRET_FILE_EXTENSIONS or ext in SECRET_FILE_NAMES, (
            f"Expected {ext!r} in SECRET_FILE_EXTENSIONS or SECRET_FILE_NAMES"
        )

    @pytest.mark.parametrize("ext", [".pem", ".key", ".pfx", ".p12"])
    def test_extensions_in_secret_file_extensions(self, ext: str) -> None:
        assert ext in SECRET_FILE_EXTENSIONS, f"Expected {ext!r} in SECRET_FILE_EXTENSIONS"

    def test_immutable(self) -> None:
        with pytest.raises(AttributeError):
            SECRET_FILE_EXTENSIONS.add("should_fail")  # type: ignore[attr-defined]
