"""Tests for the user-facing localization boundary."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, cast

import pytest

from hephaestus.cli.localization import Localizer, get_localizer, text, using_localizer


def test_english_fallback_and_missing_key() -> None:
    """English source text is the complete fallback."""
    assert text("Uncatalogued message") == "Uncatalogued message"
    assert Localizer({"Known": "Connu"}).text("Missing") == "Missing"


def test_synthetic_catalog_and_placeholders() -> None:
    """Catalogs translate named and positional templates."""
    localizer = Localizer(
        {
            "Hello %(name)s": "Bonjour %(name)s",
            "Processed %d files": "%d fichiers traités",
        }
    )
    assert localizer.text("Hello %(name)s", name="Ada") == "Bonjour Ada"
    assert localizer.text("Processed %d files", 3) == "3 fichiers traités"


def test_named_placeholders_may_be_reordered() -> None:
    """Translations may use named values in natural target-language order."""
    localizer = Localizer({"%(first)s then %(second)d": "%(second)d avant %(first)s"})
    assert localizer.text("%(first)s then %(second)d", first="one", second=2) == "2 avant one"


@pytest.mark.parametrize(
    ("source", "translated"),
    [
        ("Hello %(name)s", "Bonjour"),
        ("Hello", "Bonjour %(name)s"),
        ("Hello %(name)s", "Bonjour %(other)s"),
        ("Count %(count)d", "Compte %(count)s"),
        ("%s has %d", "%d a %s"),
        ("Width %*s", "Large %s"),
        ("Precision %.*f", "Précision %f"),
        ("Number %*.*f", "Nombre %.*f"),
    ],
)
def test_invalid_placeholder_catalogs_are_rejected(source: str, translated: str) -> None:
    """Translations must preserve placeholder names, types, and order."""
    with pytest.raises(ValueError, match="placeholder"):
        Localizer({source: translated})


@pytest.mark.parametrize(
    ("source", "translated"),
    [
        ("Value %s", "Valeur %s %q"),
        ("Value %q", "Valeur"),
        ("Count %(count)d", "Compte %(count)d %q"),
    ],
)
def test_malformed_percent_specifiers_are_rejected_at_catalog_construction(
    source: str,
    translated: str,
) -> None:
    """A malformed percent token cannot survive catalog validation to fail rendering."""
    with pytest.raises(ValueError):
        Localizer({source: translated})


@pytest.mark.parametrize("catalog", [[], (), 0, False])
def test_falsy_non_mapping_catalogs_are_rejected(catalog: object) -> None:
    """Only ``None`` selects the empty catalog; falsy non-mappings are invalid input."""
    with pytest.raises(TypeError, match="mapping"):
        Localizer(cast(Any, catalog))


def test_escaped_percent_is_not_a_placeholder() -> None:
    """Escaped percent signs may differ without changing the value signature."""
    localizer = Localizer({"Progress: %(value)d%%": "Avancement : %(value)d %%"})
    assert localizer.text("Progress: %(value)d%%", value=50) == "Avancement : 50 %"


def test_catalog_is_defensively_copied_and_localizer_is_immutable() -> None:
    """Caller mutations cannot alter an existing localizer."""
    catalog = {"Hello": "Bonjour"}
    localizer = Localizer(catalog)
    catalog["Hello"] = "Salut"
    assert localizer.text("Hello") == "Bonjour"
    with pytest.raises(AttributeError):
        localizer._catalog = {}  # type: ignore[misc]


def test_context_nesting_and_exception_restoration() -> None:
    """Scoped catalogs restore the prior localizer in all exit paths."""
    original = get_localizer()
    with using_localizer({"Hello": "Bonjour"}) as outer:
        assert get_localizer() is outer
        with pytest.raises(RuntimeError):
            with using_localizer({"Hello": "Hola"}):
                assert text("Hello") == "Hola"
                raise RuntimeError("stop")
        assert text("Hello") == "Bonjour"
    assert get_localizer() is original


def _ordinary_parser_help(barrier: Barrier) -> str:
    parser = argparse.ArgumentParser(description="Application help")
    barrier.wait()
    return parser.format_help()


def test_argparse_metadata_is_explicit_and_concurrently_isolated() -> None:
    """Localization does not mutate argparse's process-global dispatchers."""
    argparse_module = cast(Any, argparse)
    before = (argparse_module._, argparse_module.ngettext)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with using_localizer({"Application help": "Aide application"}):
            localized = argparse.ArgumentParser(description=text("Application help"))
            ordinary_help = executor.submit(_ordinary_parser_help, barrier)
            barrier.wait()
            assert "Aide application" in localized.format_help()
            assert "Application help" in ordinary_help.result()
    assert (argparse_module._, argparse_module.ngettext) == before


def test_argparse_syntax_version_and_exit_codes_are_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only authored display metadata crosses the localization boundary."""
    with using_localizer({"Choose a mode": "Choisissez un mode"}):
        parser = argparse.ArgumentParser(prog="tool", description=text("Choose a mode"))
        parser.add_argument("--mode", dest="selected", metavar="MODE", choices=("a", "b"))
        parser.add_argument("--version", action="version", version="tool 1.2.3")

    args = parser.parse_args(["--mode", "a"])
    assert args.selected == "a"
    help_text = parser.format_help()
    assert "--mode MODE" in help_text
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out == "tool 1.2.3\n"


def test_mixed_positional_and_named_values_are_rejected() -> None:
    """Formatting styles cannot be mixed in one rendering call."""
    localizer = Localizer()
    with pytest.raises(TypeError, match="cannot mix"):
        localizer.text("%s %(name)s", "value", name="other")
