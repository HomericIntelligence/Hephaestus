"""Tests for the user-facing localization boundary."""

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

import hephaestus.cli as cli
from hephaestus._localization import _placeholder_signature
from hephaestus.cli.localization import Localizer, get_localizer, text, using_localizer


def test_english_fallback_and_missing_key() -> None:
    """Use the English source text when a catalog has no entry."""
    assert text("Uncatalogued message") == "Uncatalogued message"
    assert Localizer({"Known": "Connu"}).text("Missing") == "Missing"


def test_synthetic_catalog_and_placeholders() -> None:
    """Translate a stable template and then insert its named values."""
    localizer = Localizer({"Hello %(name)s": "Bonjour %(name)s"})

    assert localizer.text("Hello %(name)s", name="Ada") == "Bonjour Ada"


def test_positional_placeholder_rendering() -> None:
    """Translate and render positional placeholders in their original order."""
    localizer = Localizer({"%s processed %d files": "%s a traité %d fichiers"})

    assert localizer.text("%s processed %d files", "Ada", 2) == "Ada a traité 2 fichiers"


def test_rendering_rejects_mixed_argument_styles() -> None:
    """Reject a call that supplies positional and named formatting values."""
    with pytest.raises(TypeError, match="cannot mix"):
        Localizer().text("Hello", "Ada", name="Ada")


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("Width %*s", (("*", "s"), ())),
        ("Precision %.*f", (("*", "f"), ())),
        ("Width and precision %*.*f", (("*", "*", "f"), ())),
    ],
)
def test_placeholder_signature_includes_star_operands(
    template: str,
    expected: tuple[tuple[str, ...], tuple[tuple[str, str], ...]],
) -> None:
    """Count star width and precision operands before their value."""
    assert _placeholder_signature(template) == expected


@pytest.mark.parametrize(
    ("source", "translated"),
    [
        ("Hello %(name)s", "Bonjour"),
        ("Hello", "Bonjour %(name)s"),
        ("Hello %(name)s", "Bonjour %(other)s"),
        ("Count %(count)d", "Compte %(count)s"),
    ],
)
def test_invalid_placeholder_catalogs_are_rejected(source: str, translated: str) -> None:
    """Reject translations that change the placeholder contract."""
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
def test_malformed_percent_tokens_are_rejected(source: str, translated: str) -> None:
    """Reject malformed tokens when the catalog is constructed."""
    with pytest.raises(ValueError):
        Localizer({source: translated})


@pytest.mark.parametrize("catalog", [[], (), 0, False])
def test_falsy_non_mapping_catalogs_are_rejected(catalog: object) -> None:
    """Accept only mappings or ``None`` as catalog input."""
    with pytest.raises(TypeError, match="mapping"):
        Localizer(cast(Any, catalog))


def test_escaped_percent_is_not_a_placeholder() -> None:
    """Permit a literal percent escape in a formatted template."""
    localizer = Localizer({"Progress: %(value)d%%": "Avancement : %(value)d %%"})

    assert localizer.text("Progress: %(value)d%%", value=50) == "Avancement : 50 %"


def test_literal_percent_without_format_values_is_catalogable() -> None:
    """Render escaped literal percent text without formatting values."""
    localizer = Localizer(
        {
            "Coverage is 100%% and the expected pattern is <N>%%+.": (
                "La couverture est de 100%% et le modèle attendu est <N>%%+."
            )
        }
    )

    assert localizer.text("Coverage is 100%% and the expected pattern is <N>%%+.") == (
        "La couverture est de 100% et le modèle attendu est <N>%+."
    )


def test_space_flag_placeholder_followed_by_text_cannot_be_removed() -> None:
    """Preserve a valid space-flag operand when alphabetic text follows it."""
    with pytest.raises(ValueError, match="placeholder"):
        Localizer({"Count: % ditems": "Compte : éléments"})


@pytest.mark.parametrize("source", ["Step 2%(name)s", "Step 2%s"])
def test_placeholder_after_digit_cannot_be_removed(source: str) -> None:
    """Preserve a valid placeholder when a digit precedes its percent sign."""
    with pytest.raises(ValueError, match="placeholder"):
        Localizer({source: "Étape 2"})


@pytest.mark.parametrize("template", ["%s %(name)s", "%(name)*s"])
def test_catalog_rejects_mixed_formatting_styles(template: str) -> None:
    """Reject templates that cannot use one formatting argument style."""
    with pytest.raises(ValueError, match="mix"):
        Localizer({template: template})


def test_catalog_is_defensively_copied_and_localizer_is_immutable() -> None:
    """Do not let caller mutations change an existing localizer."""
    catalog = {"Hello": "Bonjour"}
    localizer = Localizer(catalog)
    catalog["Hello"] = "Salut"

    assert localizer.text("Hello") == "Bonjour"
    with pytest.raises(AttributeError):
        localizer._catalog = {}  # type: ignore[misc]


def test_context_nesting_and_exception_restoration() -> None:
    """Restore the prior localizer after all context exit paths."""
    previous_localizer = get_localizer()
    with using_localizer({"Hello": "Bonjour"}) as outer:
        assert get_localizer() is outer
        with pytest.raises(RuntimeError):
            with using_localizer({"Hello": "Hola"}):
                assert text("Hello") == "Hola"
                raise RuntimeError("stop")
        assert text("Hello") == "Bonjour"
    assert get_localizer() is previous_localizer


def test_concurrent_catalogs_are_isolated() -> None:
    """Keep each concurrent context catalog separate."""

    def render(catalog: dict[str, str]) -> str:
        with using_localizer(catalog):
            return text("Hello")

    with ThreadPoolExecutor(max_workers=2) as executor:
        french = executor.submit(render, {"Hello": "Bonjour"})
        spanish = executor.submit(render, {"Hello": "Hola"})

    assert french.result() == "Bonjour"
    assert spanish.result() == "Hola"


def test_argparse_translation_does_not_mutate_global_dispatchers() -> None:
    """Keep argparse translation dispatchers unchanged."""
    argparse_module = cast(Any, argparse)
    before = (argparse_module._, argparse_module.ngettext)

    with using_localizer({"Application help": "Aide application"}):
        parser = argparse.ArgumentParser(description=text("Application help"))

    assert "Aide application" in parser.format_help()
    assert (argparse_module._, argparse_module.ngettext) == before


def test_localized_parser_keeps_syntax_version_and_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Translate metadata and keep parser control values unchanged."""
    with using_localizer({"Application help": "Aide application"}):
        parser = argparse.ArgumentParser(description=text("Application help"), prog="demo")
        parser.add_argument("--mode", metavar="KIND", choices=("fast", "safe"))
        parser.add_argument("--version", action="version", version="demo 1.2.3")

    assert parser.parse_args(["--mode", "fast"]) == argparse.Namespace(mode="fast")
    assert "--mode KIND" in parser.format_help()
    with pytest.raises(SystemExit) as version_exit:
        parser.parse_args(["--version"])
    assert version_exit.value.code == 0
    assert capsys.readouterr().out == "demo 1.2.3\n"
    with pytest.raises(SystemExit) as syntax_exit:
        parser.parse_args(["--mode", "other"])
    assert syntax_exit.value.code == 2


def test_localized_parser_construction_does_not_change_concurrent_parser() -> None:
    """Keep an ordinary parser in English during localized construction."""
    argparse_module = cast(Any, argparse)
    before = (argparse_module._, argparse_module.ngettext)
    barrier = threading.Barrier(2)

    def ordinary_help() -> str:
        barrier.wait()
        return argparse.ArgumentParser(description="Application help").format_help()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ordinary_help)
        with using_localizer({"Application help": "Aide application"}):
            localized = argparse.ArgumentParser(description=text("Application help"))
            barrier.wait()
        ordinary = future.result()

    assert "Aide application" in localized.format_help()
    assert "Application help" in ordinary
    assert "Aide application" not in ordinary
    assert (argparse_module._, argparse_module.ngettext) == before


def test_cli_exposes_localizer() -> None:
    """Expose the localization boundary from the CLI package."""
    assert cli.Localizer is Localizer
