"""Integration tests for localized parser construction."""

import argparse

import pytest

from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.cli.localization import text, using_localizer
from hephaestus.cli.utils import create_validation_parser

pytestmark = pytest.mark.integration


def test_direct_grouped_and_subparser_metadata_translation() -> None:
    """Translate authored metadata without changing parser syntax."""
    catalog = {
        "Manage projects": "Gérer les projets",
        "Project options": "Options du projet",
        "Choose a project": "Choisir un projet",
        "Create a project": "Créer un projet",
    }
    with using_localizer(catalog):
        parser = argparse.ArgumentParser(description=text("Manage projects"))
        group = parser.add_argument_group(text("Project options"))
        group.add_argument("--project", metavar="NAME", help=text("Choose a project"))
        subparsers = parser.add_subparsers(dest="command")
        create = subparsers.add_parser("create", help=text("Create a project"))
        create.add_argument("--kind", choices=("public", "private"))

    help_text = parser.format_help()
    assert "Gérer les projets" in help_text
    assert "Options du projet" in help_text
    assert "Choisir un projet" in help_text
    assert "--project NAME" in help_text
    assert parser.parse_args(["--project", "demo", "create", "--kind", "public"]) == (
        argparse.Namespace(project="demo", command="create", kind="public")
    )


def test_shared_parser_factories_translate_at_the_boundary() -> None:
    """Translate descriptions, epilogs, and shared option help."""
    catalog = {
        "Validate files": "Valider les fichiers",
        "Example: %(prog)s PATH": "Exemple : %(prog)s PATH",
        "Run workers": "Lancer les ouvriers",
        "Maximum number of parallel workers, 1-32 (default: 3)": (
            "Nombre maximal d'ouvriers parallèles, 1-32 (valeur par défaut : 3)"
        ),
    }
    with using_localizer(catalog):
        validation = create_validation_parser(
            "Validate files",
            prog="validator",
            epilog="Example: %(prog)s PATH",
        )
        automation = build_automation_parser(
            "Run workers",
            add_agent=False,
            add_dry_run=False,
            add_json=False,
            add_version=False,
            add_verbose=False,
        )

    assert "Valider les fichiers" in validation.format_help()
    assert "Exemple : validator PATH" in validation.format_help()
    assert "Lancer les ouvriers" in automation.format_help()
    assert "Nombre maximal d'ouvriers parallèles" in automation.format_help()
    assert automation.parse_args(["--max-workers", "2"]).max_workers == 2
