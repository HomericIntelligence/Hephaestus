"""Integration coverage for localized CLI parser construction."""

import argparse

import pytest

from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.cli.localization import text, using_localizer
from hephaestus.cli.utils import create_validation_parser

pytestmark = pytest.mark.integration


def test_direct_grouped_and_subparser_metadata_translation() -> None:
    """Authored metadata translates without changing parser syntax."""
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


def test_shared_validation_and_automation_parsers_translate_at_boundary() -> None:
    """Shared factories translate descriptions, epilogs, and help."""
    catalog = {
        "Validate files": "Valider les fichiers",
        "Example: %(prog)s PATH": "Exemple : %(prog)s PATH",
        "Run workers": "Lancer les ouvriers",
        "Disable curses UI (use plain logging instead)": ("Désactiver l'interface curses"),
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
            add_max_workers=False,
            add_dry_run=False,
            add_no_ui=True,
            add_json=False,
            add_version=False,
            add_verbose=False,
        )

    validation_help = validation.format_help()
    automation_help = automation.format_help()
    assert "Valider les fichiers" in validation_help
    assert "Exemple : validator PATH" in validation_help
    assert "--repo-root REPO_ROOT" in validation_help
    assert "Lancer les ouvriers" in automation_help
    assert "Désactiver l'interface curses" in automation_help
    assert automation.parse_args(["--no-ui"]).no_ui is True
