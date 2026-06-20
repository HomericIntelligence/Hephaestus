"""Unit tests for the shared ``advise_runner`` module (#30).

Covers the Mnemosyne-resolution + run-advise orchestration that all three
pipeline stages share. The per-stage invokers are tested in each stage's own
suite; here we exercise the shared logic with a stub invoker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation import advise_runner


def _build_prompt(**kwargs: object) -> str:
    """Stand-in for prompts.get_advise_prompt — echoes the marketplace path."""
    return f"ADVISE marketplace={kwargs['marketplace_path']}"


# ---------------------------------------------------------------------------
# advise_skipped
# ---------------------------------------------------------------------------


class TestAdviseSkipped:
    """The advise skip-marker convention."""

    def test_marker_format(self) -> None:
        assert advise_runner.advise_skipped("boom") == "<!-- advise step skipped: boom -->"

    def test_default_mnemosyne_root_uses_agent_brain(self, tmp_path: Path) -> None:
        with patch.object(Path, "home", return_value=tmp_path):
            assert (
                advise_runner.default_mnemosyne_root()
                == tmp_path / ".agent-brain" / "ProjectMnemosyne"
            )


# ---------------------------------------------------------------------------
# ensure_mnemosyne
# ---------------------------------------------------------------------------


class TestEnsureMnemosyne:
    """ProjectMnemosyne checkout setup and recovery."""

    def test_existing_valid_checkout_is_pulled(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("hephaestus.automation.advise_runner.subprocess.run", side_effect=fake_run):
            assert advise_runner.ensure_mnemosyne(mnemosyne_root) is True

        assert ["git", "-C", str(mnemosyne_root), "pull", "--ff-only"] in calls
        assert not any(call[:3] == ["gh", "repo", "clone"] for call in calls)

    def test_existing_corrupt_checkout_is_removed_and_recloned(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()
        (mnemosyne_root / ".git").mkdir()
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a repo")
            if argv[:3] == ["gh", "repo", "clone"]:
                mnemosyne_root.mkdir(exist_ok=True)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("hephaestus.automation.advise_runner.subprocess.run", side_effect=fake_run):
            assert advise_runner.ensure_mnemosyne(mnemosyne_root) is True

        assert any("rev-parse" in call for call in calls)
        assert any(call[:3] == ["gh", "repo", "clone"] for call in calls)
        assert not any("pull" in call for call in calls)


# ---------------------------------------------------------------------------
# resolve_marketplace
# ---------------------------------------------------------------------------


class TestResolveMarketplace:
    """resolve_marketplace clone/refresh/recovery paths + skip reasons."""

    def test_returns_path_when_present(self) -> None:
        with patch.object(Path, "exists", return_value=True):
            path, reason = advise_runner.resolve_marketplace(
                Path("/home/user/.agent-brain/ProjectMnemosyne")
            )
        assert path is not None
        assert path.name == "marketplace.json"
        assert reason == ""

    def test_missing_dir_and_clone_fails(self) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(advise_runner, "ensure_mnemosyne", return_value=False) as ensure,
        ):
            path, reason = advise_runner.resolve_marketplace(
                Path("/home/user/.agent-brain/ProjectMnemosyne")
            )
        assert path is None
        assert reason == "ProjectMnemosyne unavailable"
        ensure.assert_called_once()

    def test_missing_marketplace_reclone_succeeds(self) -> None:
        calls: list[Path] = []

        def exists(self: Path) -> bool:
            calls.append(self)
            if self.name == "ProjectMnemosyne":
                return True
            # marketplace.json: absent first, present after reclone
            return calls.count(self) > 1

        with (
            patch.object(Path, "exists", exists),
            patch("hephaestus.automation.advise_runner.shutil.rmtree") as rmtree,
            patch.object(advise_runner, "ensure_mnemosyne", return_value=True) as ensure,
        ):
            path, reason = advise_runner.resolve_marketplace(
                Path("/home/user/.agent-brain/ProjectMnemosyne")
            )
        assert path is not None
        assert reason == ""
        rmtree.assert_called_once()
        ensure.assert_called_once()

    def test_missing_marketplace_reclone_fails(self) -> None:
        def exists(self: Path) -> bool:
            if self.name == "ProjectMnemosyne":
                return True
            return self.name != "marketplace.json"

        with (
            patch.object(Path, "exists", exists),
            patch("hephaestus.automation.advise_runner.shutil.rmtree") as rmtree,
            patch.object(advise_runner, "ensure_mnemosyne", return_value=False) as ensure,
        ):
            path, reason = advise_runner.resolve_marketplace(
                Path("/home/user/.agent-brain/ProjectMnemosyne")
            )
        assert path is None
        assert "marketplace.json missing" in reason
        rmtree.assert_called_once()
        ensure.assert_called_once()


# ---------------------------------------------------------------------------
# run_advise
# ---------------------------------------------------------------------------


class TestRunAdvise:
    """run_advise orchestration: success, skip, and fail-safe degradation."""

    def test_returns_selected_skill_context_on_success(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        skills_dir = mnemosyne_root / "skills"
        skills_dir.mkdir(parents=True)
        skill_file = skills_dir / "debugging.md"
        skill_file.write_text("# Debugging\n\nUse tight repros.\n", encoding="utf-8")

        with (
            patch.object(advise_runner, "get_repo_root", return_value=Path("/repo")),
            patch.object(advise_runner, "default_mnemosyne_root", return_value=mnemosyne_root),
            patch.object(
                advise_runner,
                "resolve_marketplace",
                return_value=(
                    mnemosyne_root / ".claude-plugin" / "marketplace.json",
                    "",
                ),
            ),
        ):
            captured: list[str] = []

            def invoke(prompt: str) -> str:
                captured.append(prompt)
                return (
                    '{"skills": [{"name": "debugging", "source": "./skills/debugging.md", '
                    '"reason": "Relevant to frozen automation loops."}]}'
                )

            result = advise_runner.run_advise(
                issue_number=7,
                issue_title="t",
                issue_body="b",
                invoke=invoke,
                build_prompt=_build_prompt,
            )
        assert "## Selected Team Skills" in result
        assert "### debugging" in result
        assert "Relevant to frozen automation loops." in result
        assert "Use tight repros." in result
        # The marketplace path is threaded into the prompt builder + invoker.
        assert captured and "marketplace.json" in captured[0]

    def test_skips_when_marketplace_unresolved(self) -> None:
        with (
            patch.object(advise_runner, "get_repo_root", return_value=Path("/repo")),
            patch.object(
                advise_runner,
                "resolve_marketplace",
                return_value=(None, "ProjectMnemosyne unavailable"),
            ),
        ):
            called = False

            def invoke(_prompt: str) -> str:
                nonlocal called
                called = True
                return "should not run"

            result = advise_runner.run_advise(
                issue_number=7,
                issue_title="t",
                issue_body="b",
                invoke=invoke,
                build_prompt=_build_prompt,
            )
        assert result == "<!-- advise step skipped: ProjectMnemosyne unavailable -->"
        assert called is False

    def test_degrades_to_skip_on_exception(self) -> None:
        with patch.object(advise_runner, "get_repo_root", side_effect=RuntimeError("git boom")):
            result = advise_runner.run_advise(
                issue_number=7,
                issue_title="t",
                issue_body="b",
                invoke=lambda _p: "x",
                build_prompt=_build_prompt,
            )
        assert result.startswith("<!-- advise step skipped:")
        assert "git boom" in result

    def test_rejects_selected_skill_outside_skills_tree(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        (mnemosyne_root / "skills").mkdir(parents=True)
        outside = mnemosyne_root / "README.md"
        outside.write_text("not a skill", encoding="utf-8")

        selected = advise_runner.parse_selected_skills(
            '{"skills": [{"name": "bad", "source": "./README.md", "reason": "x"}]}',
            mnemosyne_root,
        )

        assert selected == []

    def test_rejects_selected_skill_parent_traversal(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        (mnemosyne_root / "skills").mkdir(parents=True)

        selected = advise_runner.parse_selected_skills(
            '{"skills": [{"name": "bad", "source": "../secret.md", "reason": "x"}]}',
            mnemosyne_root,
        )

        assert selected == []

    def test_selected_skill_context_is_bounded(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        skills_dir = mnemosyne_root / "skills"
        skills_dir.mkdir(parents=True)
        skill_file = skills_dir / "large.md"
        skill_file.write_text("x" * 500, encoding="utf-8")
        selected = [
            advise_runner.SelectedSkill(
                name="large",
                source="./skills/large.md",
                reason="large context",
                path=skill_file,
            )
        ]

        result = advise_runner.format_selected_skill_context(selected, max_chars=260)

        assert "[truncated]" in result
        assert len(result) <= 260


# ---------------------------------------------------------------------------
# _extract_json_object — robustness against common malformed selector output
# ---------------------------------------------------------------------------


class TestExtractJsonObject:
    """The selector model sometimes returns JSON that is not strictly valid.

    These cover the shapes seen in real automation-loop runs (issue #1556):
    markdown fences, Python-style single quotes, and trailing commas. The
    extractor must recover rather than raising ``invalid selector JSON``.
    """

    def test_plain_object(self) -> None:
        assert advise_runner._extract_json_object('{"skills": []}') == {"skills": []}

    def test_json_fenced_block(self) -> None:
        text = '```json\n{"skills": [{"name": "a"}]}\n```'
        assert advise_runner._extract_json_object(text) == {"skills": [{"name": "a"}]}

    def test_bare_fenced_block(self) -> None:
        text = '```\n{"skills": []}\n```'
        assert advise_runner._extract_json_object(text) == {"skills": []}

    def test_prose_prefix_before_object(self) -> None:
        text = 'Here is the selection:\n{"skills": []}'
        assert advise_runner._extract_json_object(text) == {"skills": []}

    def test_single_quoted_object(self) -> None:
        # Python-style dict literal: the failure shape from issue #1556's log
        # ("Expecting property name enclosed in double quotes ... char 1").
        text = "{'skills': [{'name': 'a', 'reason': 'b'}]}"
        assert advise_runner._extract_json_object(text) == {
            "skills": [{"name": "a", "reason": "b"}]
        }

    def test_trailing_comma_object(self) -> None:
        text = '{"skills": [{"name": "a"},],}'
        assert advise_runner._extract_json_object(text) == {"skills": [{"name": "a"}]}

    def test_single_quoted_inside_fence(self) -> None:
        text = "```json\n{'skills': []}\n```"
        assert advise_runner._extract_json_object(text) == {"skills": []}

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty selector output"):
            advise_runner._extract_json_object("   ")

    def test_no_object_raises(self) -> None:
        with pytest.raises(ValueError, match="did not contain a JSON object"):
            advise_runner._extract_json_object("no braces here")

    def test_non_object_json_raises(self) -> None:
        with pytest.raises(ValueError, match="must be an object"):
            advise_runner._extract_json_object("[1, 2, 3]")
