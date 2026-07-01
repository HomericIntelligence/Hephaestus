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
from hephaestus.github.mnemosyne_repo import MnemosyneTarget


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

    def test_existing_checkout_uses_env_configured_git_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git validation/refresh calls use the centralized call-time timeout."""
        monkeypatch.setenv("HEPH_AGENT_GIT_TIMEOUT", "44")
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()
        timeouts: list[object] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            timeouts.append(kwargs.get("timeout"))
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="true\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("hephaestus.automation.advise_runner.subprocess.run", side_effect=fake_run):
            assert advise_runner.ensure_mnemosyne(mnemosyne_root) is True

        assert timeouts == [44, 44]

    def test_clone_uses_env_configured_clone_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ProjectMnemosyne clone calls use the centralized clone timeout."""
        monkeypatch.setenv("HEPH_AGENT_CLONE_TIMEOUT", "55")
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        target = MnemosyneTarget(
            owner="HomericIntelligence",
            slug="HomericIntelligence/ProjectMnemosyne",
            is_fork_of_upstream=False,
        )

        with (
            patch("hephaestus.automation.advise_runner.gh_call") as gh_call,
            patch(
                "hephaestus.automation.advise_runner.resolve_mnemosyne_target",
                return_value=target,
            ),
        ):
            gh_call.return_value = subprocess.CompletedProcess(
                ["gh", "repo", "clone"], 0, stdout="", stderr=""
            )
            assert advise_runner._clone_mnemosyne(mnemosyne_root) is True

        assert gh_call.call_args.kwargs["timeout"] == 55
        # The clone targets the resolved slug, not a hardcoded upstream literal.
        assert gh_call.call_args[0][0][:3] == ["repo", "clone", target.slug]

    def test_existing_corrupt_checkout_is_removed_and_recloned(self, tmp_path: Path) -> None:
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        mnemosyne_root.mkdir()
        (mnemosyne_root / ".git").mkdir()
        calls: list[list[str]] = []
        gh_calls: list[list[str]] = []
        target = MnemosyneTarget(
            owner="HomericIntelligence",
            slug="HomericIntelligence/ProjectMnemosyne",
            is_fork_of_upstream=False,
        )

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a repo")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        def fake_gh_call(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            gh_calls.append(argv)
            mnemosyne_root.mkdir(exist_ok=True)
            return subprocess.CompletedProcess(["gh", *argv], 0, stdout="", stderr="")

        with (
            patch("hephaestus.automation.advise_runner.subprocess.run", side_effect=fake_run),
            patch("hephaestus.automation.advise_runner.gh_call", side_effect=fake_gh_call),
            patch(
                "hephaestus.automation.advise_runner.resolve_mnemosyne_target",
                return_value=target,
            ),
        ):
            assert advise_runner.ensure_mnemosyne(mnemosyne_root) is True

        assert any("rev-parse" in call for call in calls)
        assert gh_calls == [["repo", "clone", target.slug, str(mnemosyne_root)]]
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

    def test_retries_once_on_unparseable_selector_output(self, tmp_path: Path) -> None:
        """#1587: a non-JSON first selector response is re-asked once, then parsed."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        skills_dir = mnemosyne_root / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "debugging.md").write_text("# Debugging\n", encoding="utf-8")

        with (
            patch.object(advise_runner, "get_repo_root", return_value=Path("/repo")),
            patch.object(advise_runner, "default_mnemosyne_root", return_value=mnemosyne_root),
            patch.object(
                advise_runner,
                "resolve_marketplace",
                return_value=(mnemosyne_root / ".claude-plugin" / "marketplace.json", ""),
            ),
        ):
            calls: list[str] = []

            def invoke(prompt: str) -> str:
                calls.append(prompt)
                if len(calls) == 1:
                    return "Sure! Here are the skills you should use: (prose, no JSON)"
                return (
                    '{"skills": [{"name": "debugging", '
                    '"source": "./skills/debugging.md", "reason": "r"}]}'
                )

            result = advise_runner.run_advise(
                issue_number=7,
                issue_title="t",
                issue_body="b",
                invoke=invoke,
                build_prompt=_build_prompt,
            )
        assert len(calls) == 2  # retried once
        assert "JSON object" in calls[1]  # retry prompt carries the JSON-only reminder
        assert "### debugging" in result

    def test_unparseable_twice_degrades_to_skip(self, tmp_path: Path) -> None:
        """#1587: if the retry is ALSO unparseable, degrade to a skip (no abort)."""
        mnemosyne_root = tmp_path / "ProjectMnemosyne"
        (mnemosyne_root / "skills").mkdir(parents=True)
        with (
            patch.object(advise_runner, "get_repo_root", return_value=Path("/repo")),
            patch.object(advise_runner, "default_mnemosyne_root", return_value=mnemosyne_root),
            patch.object(
                advise_runner,
                "resolve_marketplace",
                return_value=(mnemosyne_root / ".claude-plugin" / "marketplace.json", ""),
            ),
        ):
            result = advise_runner.run_advise(
                issue_number=7,
                issue_title="t",
                issue_body="b",
                invoke=lambda _p: "still no json",
                build_prompt=_build_prompt,
            )
        assert result.startswith("<!-- advise step skipped:")

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
