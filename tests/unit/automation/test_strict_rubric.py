"""Unit tests for the strict-rubric skill-path parameterization (issue #800)."""

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.prompts import _strict_rubric as sr


def test_skill_reference_uses_env_var_when_set(tmp_path, monkeypatch):
    """When HEPHAESTUS_PLUGIN_SKILLS_DIR is set, _skill_reference() uses it."""
    skill_dir = tmp_path / "skills"
    skill_path = skill_dir / "review-pr-strict" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("stub")
    monkeypatch.setenv("HEPHAESTUS_PLUGIN_SKILLS_DIR", str(skill_dir))
    assert str(skill_path) in sr._skill_reference()


def test_skill_reference_default_graceful_degradation(monkeypatch, tmp_path):
    """When SKILL.md is absent, _skill_reference() returns empty string."""
    monkeypatch.delenv("HEPHAESTUS_PLUGIN_SKILLS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert sr._skill_reference() == ""


def test_rubric_contains_no_hardcoded_home_path(monkeypatch, tmp_path):
    """Rubric prompt must not embed hardcoded ~/... or /home/... paths."""
    monkeypatch.delenv("HEPHAESTUS_PLUGIN_SKILLS_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    rubric = sr.build_strict_review_rubric()
    assert "~/.claude/plugins" not in rubric
    assert "/home/" not in rubric


def test_rubric_includes_skill_path_when_file_exists(tmp_path, monkeypatch):
    """When SKILL.md exists, rubric prompt includes the resolved path."""
    skill_dir = tmp_path / "skills"
    skill_md = skill_dir / "review-pr-strict" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("stub")
    monkeypatch.setenv("HEPHAESTUS_PLUGIN_SKILLS_DIR", str(skill_dir))
    rubric = sr.build_strict_review_rubric()
    assert str(skill_md) in rubric


def test_module_level_constant_still_exported():
    """Backward-compat: _STRICT_REVIEW_RUBRIC symbol still resolves."""
    from hephaestus.automation.prompts import _STRICT_REVIEW_RUBRIC

    assert "ruthlessly thorough technical reviewer" in _STRICT_REVIEW_RUBRIC
