"""Unit tests for the shared ``advise_runner`` module (#30).

Covers the Mnemosyne-resolution + run-advise orchestration that all three
pipeline stages share. The per-stage invokers are tested in each stage's own
suite; here we exercise the shared logic with a stub invoker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

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
            path, reason = advise_runner.resolve_marketplace(Path("/repo/build/ProjectMnemosyne"))
        assert path is not None
        assert path.name == "marketplace.json"
        assert reason == ""

    def test_missing_dir_and_clone_fails(self) -> None:
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(advise_runner, "ensure_mnemosyne", return_value=False) as ensure,
        ):
            path, reason = advise_runner.resolve_marketplace(Path("/repo/build/ProjectMnemosyne"))
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
            path, reason = advise_runner.resolve_marketplace(Path("/repo/build/ProjectMnemosyne"))
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
            path, reason = advise_runner.resolve_marketplace(Path("/repo/build/ProjectMnemosyne"))
        assert path is None
        assert "marketplace.json missing" in reason
        rmtree.assert_called_once()
        ensure.assert_called_once()


# ---------------------------------------------------------------------------
# run_advise
# ---------------------------------------------------------------------------


class TestRunAdvise:
    """run_advise orchestration: success, skip, and fail-safe degradation."""

    def test_returns_invoker_output_on_success(self) -> None:
        with (
            patch.object(advise_runner, "get_repo_root", return_value=Path("/repo")),
            patch.object(
                advise_runner,
                "resolve_marketplace",
                return_value=(
                    Path("/repo/build/ProjectMnemosyne/.claude-plugin/marketplace.json"),
                    "",
                ),
            ),
        ):
            captured: list[str] = []

            def invoke(prompt: str) -> str:
                captured.append(prompt)
                return "## Findings\n- be careful"

            result = advise_runner.run_advise(
                issue_number=7,
                issue_title="t",
                issue_body="b",
                invoke=invoke,
                build_prompt=_build_prompt,
            )
        assert result == "## Findings\n- be careful"
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
