"""Validate issue forms + severity tagger wiring feed the pipeline (#1210)."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
FORMS = ["feature_request.yml", "bug_report.yml"]

SEVERITY_OPTIONS = ["critical", "major", "minor", "nitpick"]
PROVISIONED_DEFAULT_LABELS = {"enhancement", "bug"}


def _load(form: str) -> dict:
    return yaml.safe_load((TEMPLATE_DIR / form).read_text(encoding="utf-8"))


def _field(data: dict, field_id: str) -> dict:
    for item in data["body"]:
        if item.get("id") == field_id:
            return item
    raise AssertionError(f"missing field id={field_id!r}")


def test_forms_are_valid_yaml_with_body() -> None:
    """Both issue forms parse as YAML with a list-valued ``body``."""
    for form in FORMS:
        assert isinstance(_load(form).get("body"), list)


def test_severity_dropdown_schema_valid_no_default() -> None:
    """Severity is an optional dropdown with all four options and no brittle default."""
    for form in FORMS:
        sev = _field(_load(form), "severity")
        assert sev["type"] == "dropdown"
        opts = sev["attributes"]["options"]
        assert opts and all(o in opts for o in SEVERITY_OPTIONS)
        assert "default" not in sev["attributes"]
        assert sev.get("validations", {}).get("required", False) is False


def test_parent_epic_is_optional_input() -> None:
    """Parent Epic is an optional free-text ``input`` (reference only)."""
    for form in FORMS:
        p = _field(_load(form), "parent_epic")
        assert p["type"] == "input"
        assert p.get("validations", {}).get("required", False) is False


def test_acceptance_criteria_is_required_checklist_textarea() -> None:
    """Both forms require testable completion criteria in checklist form."""
    for form in FORMS:
        criteria = _field(_load(form), "acceptance_criteria")
        assert criteria["type"] == "textarea"
        assert criteria["attributes"]["label"] == "Acceptance Criteria"
        assert "- [ ]" in criteria["attributes"]["placeholder"]
        assert criteria["validations"]["required"] is True


def test_verification_plan_is_required_criterion_map() -> None:
    """Both forms require criterion-linked verification and expected evidence."""
    for form in FORMS:
        plan = _field(_load(form), "verification_plan")
        assert plan["type"] == "textarea"
        assert plan["attributes"]["label"] == "Verification Plan"
        assert plan["validations"]["required"] is True

        guidance = " ".join(
            (
                plan["attributes"]["description"],
                plan["attributes"]["placeholder"],
            )
        ).lower()
        assert "acceptance criterion" in guidance
        assert "command" in guidance
        assert "expected evidence" in guidance


def test_forms_seed_only_existing_labels() -> None:
    """Forms seed only provisioned default labels (no phantom labels)."""
    for form in FORMS:
        for lbl in _load(form).get("labels", []):
            assert lbl in PROVISIONED_DEFAULT_LABELS, f"{form} seeds unknown {lbl!r}"


def test_forms_document_auto_state_label() -> None:
    """A markdown block documents the automatic ``state:needs-plan`` labelling."""
    for form in FORMS:
        md = " ".join(
            i["attributes"]["value"] for i in _load(form)["body"] if i.get("type") == "markdown"
        )
        assert "state:needs-plan" in md
