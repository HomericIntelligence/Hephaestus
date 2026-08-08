"""Tests for hephaestus.discovery.skills."""

from __future__ import annotations

from pathlib import Path

from hephaestus.discovery.skills import discover_skills, get_skill_category, organize_skills

_MAPPINGS = {
    "github": ["gh-review-pr", "gh-create-pr"],
    "workflow": ["phase-plan", "phase-implement"],
}


class TestGetSkillCategory:
    """Tests for get_skill_category()."""

    def test_explicit_mapping(self) -> None:
        assert get_skill_category("gh-review-pr", _MAPPINGS) == "github"

    def test_prefix_fallback_gh(self) -> None:
        assert get_skill_category("gh-unknown") == "github"

    def test_prefix_fallback_mojo(self) -> None:
        assert get_skill_category("mojo-format") == "mojo"

    def test_prefix_fallback_phase(self) -> None:
        assert get_skill_category("phase-plan") == "workflow"

    def test_prefix_fallback_quality(self) -> None:
        assert get_skill_category("quality-check") == "quality"

    def test_prefix_fallback_worktree(self) -> None:
        assert get_skill_category("worktree-create") == "worktree"

    def test_prefix_fallback_doc(self) -> None:
        assert get_skill_category("doc-generate") == "documentation"

    def test_prefix_fallback_agent(self) -> None:
        assert get_skill_category("agent-run") == "agent"

    def test_unknown_falls_back_to_other(self) -> None:
        assert get_skill_category("custom-skill") == "other"

    def test_no_mappings_uses_prefix_only(self) -> None:
        assert get_skill_category("gh-review-pr") == "github"


class TestDiscoverSkills:
    """Tests for discover_skills()."""

    def test_finds_skill_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "gh-review-pr").mkdir()
        result = discover_skills(tmp_path)
        assert any(p.name == "gh-review-pr" for p in result["github"])

    def test_finds_skill_files(self, tmp_path: Path) -> None:
        (tmp_path / "my-skill.md").write_text("# skill")
        result = discover_skills(tmp_path)
        assert any(p.name == "my-skill.md" for p in result["other"])

    def test_skips_template_files(self, tmp_path: Path) -> None:
        (tmp_path / "TEMPLATE.md").write_text("# template")
        result = discover_skills(tmp_path)
        all_paths = [p for paths in result.values() for p in paths]
        assert not any(p.name == "TEMPLATE.md" for p in all_paths)

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").mkdir()
        result = discover_skills(tmp_path)
        all_paths = [p for paths in result.values() for p in paths]
        assert not any(p.name == ".hidden" for p in all_paths)

    def test_with_custom_mappings(self, tmp_path: Path) -> None:
        (tmp_path / "my-special-skill").mkdir()
        mappings = {"custom": ["my-special-skill"]}
        result = discover_skills(tmp_path, mappings)
        assert any(p.name == "my-special-skill" for p in result["custom"])

    def test_empty_directory(self, tmp_path: Path) -> None:
        result = discover_skills(tmp_path)
        assert "other" in result
        assert all(len(v) == 0 for v in result.values())


class TestOrganizeSkills:
    """Tests for organize_skills()."""

    def test_creates_category_dirs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "gh-review-pr").mkdir()
        dst = tmp_path / "dst"
        organize_skills(src, dst)
        assert (dst / "github").is_dir()

    def test_copies_skill_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        skill_dir = src / "gh-review-pr"
        skill_dir.mkdir()
        (skill_dir / "README.md").write_text("# skill")
        dst = tmp_path / "dst"
        result = organize_skills(src, dst)
        assert "gh-review-pr" in result["github"]
        assert (dst / "github" / "gh-review-pr" / "README.md").exists()

    def test_copies_skill_file(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "my-skill.md").write_text("# skill")
        dst = tmp_path / "dst"
        result = organize_skills(src, dst)
        assert "my-skill" in result["other"]
        assert (dst / "other" / "my-skill.md").exists()

    def test_returns_stats(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        result = organize_skills(src, dst)
        assert "other" in result
