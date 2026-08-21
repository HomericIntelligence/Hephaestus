"""Unit tests for deterministic session naming."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import agent_config
from hephaestus.automation.session_naming import (
    AGENT_ADDRESS_REVIEW,
    AGENT_ADVISE,
    AGENT_CI_DRIVER,
    AGENT_IMPLEMENTER,
    AGENT_LEARNINGS,
    AGENT_PLAN_REVIEWER,
    AGENT_PLANNER,
    AGENT_PR_REVIEWER,
    current_trunk_githash,
    resolve_session_jsonl_path,
    reviewer_agent,
    session_jsonl_path,
    session_name,
    session_uuid,
    short_githash,
)

_GIT_REPO_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _git_test_env() -> dict[str, str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for key in _GIT_REPO_ENV_KEYS:
        env.pop(key, None)
    return env


class TestReviewerAgent:
    """Per-iteration reviewer session tokens (fresh session each loop round)."""

    def test_plan_reviewer_iteration_token(self) -> None:
        assert reviewer_agent(AGENT_PLAN_REVIEWER, 0) == "plan-reviewer-r0"
        assert reviewer_agent(AGENT_PR_REVIEWER, 2) == "pr-reviewer-r2"

    def test_per_iteration_uuids_differ(self) -> None:
        u0 = session_uuid("R", 5, reviewer_agent(AGENT_PLAN_REVIEWER, 0))
        u1 = session_uuid("R", 5, reviewer_agent(AGENT_PLAN_REVIEWER, 1))
        assert u0 != u1

    def test_suffixed_reviewer_token_is_valid_session_agent(self) -> None:
        # session_name must accept the reviewer_agent() form.
        name = session_name("R", 5, reviewer_agent(AGENT_PR_REVIEWER, 3))
        assert name == "R_5_pr-reviewer-r3"

    def test_rejects_non_reviewer_base(self) -> None:
        with pytest.raises(ValueError, match="reviewer_agent expects"):
            reviewer_agent(AGENT_IMPLEMENTER, 0)

    def test_rejects_negative_iteration(self) -> None:
        with pytest.raises(ValueError, match="iteration must be"):
            reviewer_agent(AGENT_PLAN_REVIEWER, -1)

    def test_unsuffixed_unknown_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown agent"):
            session_name("R", 5, "totally-bogus")


class TestSessionName:
    """Human-readable session name construction.

    Per #841 the tuple is (repo, issue, agent) — no githash. The transcript
    persists across main-bumps so resume keeps working as a long-lived
    artifact is touched again.
    """

    def test_basic(self) -> None:
        assert session_name("Scylla", 1944, AGENT_PLANNER) == "Scylla_1944_planner"

    def test_strips_hash_prefix_from_issue(self) -> None:
        assert session_name("R", "#42", AGENT_PLANNER) == "R_42_planner"

    def test_int_and_str_issue_equivalent(self) -> None:
        assert session_name("R", 42, AGENT_PLANNER) == session_name("R", "42", AGENT_PLANNER)

    def test_unknown_agent_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown agent"):
            session_name("R", 1, "wizard")

    @pytest.mark.parametrize(
        ("repo", "issue"),
        [("", 1), ("R", "")],
    )
    def test_empty_components_raise(self, repo: str, issue: int | str) -> None:
        with pytest.raises(ValueError):
            session_name(repo, issue, AGENT_PLANNER)

    def test_whitespace_stripped(self) -> None:
        assert session_name("  R  ", 1, AGENT_PLANNER) == "R_1_planner"


class TestSessionUUID:
    """Deterministic UUIDv5 derivation from (repo, issue, agent)."""

    def test_deterministic(self) -> None:
        a = session_uuid("Scylla", 1944, AGENT_PLANNER)
        b = session_uuid("Scylla", 1944, AGENT_PLANNER)
        assert a == b

    def test_returns_valid_uuid(self) -> None:
        sid = session_uuid("Scylla", 1944, AGENT_PLANNER)
        # uuid.UUID raises ValueError on invalid input.
        uuid.UUID(sid)

    def test_different_agent_different_uuid(self) -> None:
        a = session_uuid("R", 1, AGENT_PLANNER)
        b = session_uuid("R", 1, AGENT_PLAN_REVIEWER)
        assert a != b

    def test_different_repo_different_uuid(self) -> None:
        assert session_uuid("R1", 1, AGENT_PLANNER) != session_uuid("R2", 1, AGENT_PLANNER)

    def test_different_issue_different_uuid(self) -> None:
        assert session_uuid("R", 1, AGENT_PLANNER) != session_uuid("R", 2, AGENT_PLANNER)

    def test_different_model_different_uuid(self) -> None:
        """#1166: the model is part of the key so sessions never cross models."""
        a = session_uuid("R", 1, AGENT_PLANNER, "claude-sonnet-4-6")
        b = session_uuid("R", 1, AGENT_PLANNER, "claude-opus-4-8")
        assert a != b

    def test_registered_worktree_family_gets_same_checkout_uuid(self, tmp_path: Path) -> None:
        """Repo-root and linked-worktree callers share their common-dir identity."""
        repo_root = tmp_path / "owner-a" / "Repo"
        worktree = tmp_path / "worktrees" / "issue-2284"
        common_dir = repo_root / ".git"
        repo_root.mkdir(parents=True)
        worktree.mkdir(parents=True)

        with patch(
            "hephaestus.automation.agent_config.subprocess.run",
            return_value=MagicMock(stdout=f"{common_dir}\n"),
        ):
            root_sid = session_uuid("Repo", 2284, AGENT_PLANNER, "fable", cwd=repo_root)
            worktree_sid = session_uuid("Repo", 2284, AGENT_PLANNER, "fable", cwd=worktree)

        assert root_sid == worktree_sid

    def test_implicit_cwd_is_checkout_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting cwd still isolates sessions when callers run in each checkout."""
        dotted = tmp_path / "owner.a" / "Repo"
        dashed = tmp_path / "owner-a" / "Repo"
        dotted.mkdir(parents=True)
        dashed.mkdir(parents=True)
        monkeypatch.setattr(
            "hephaestus.automation.agent_config._checkout_identity",
            lambda cwd: str(cwd.resolve()),
        )

        monkeypatch.chdir(dotted)
        dotted_sid = session_uuid("Repo", 2284, AGENT_PLANNER, "fable")
        monkeypatch.chdir(dashed)
        dashed_sid = session_uuid("Repo", 2284, AGENT_PLANNER, "fable")

        assert dotted_sid != dashed_sid
        assert (
            session_jsonl_path(dotted_sid, dotted).parent
            == session_jsonl_path(dashed_sid, dashed).parent
        )

    def test_omitting_model_preserves_legacy_key(self) -> None:
        """Backward compat: no model reproduces the historical (repo, issue, agent) id."""
        from hephaestus.automation.session_naming import session_name

        assert session_name("R", 1, AGENT_PLANNER) == "R_1_planner"
        assert session_name("R", 1, AGENT_PLANNER, None) == "R_1_planner"

    def test_model_token_sanitized_into_key(self) -> None:
        from hephaestus.automation.session_naming import session_name

        # Slashes/colons in a model id are normalized to a name-safe token.
        name = session_name("R", 1, AGENT_PLANNER, "us.anthropic/opus:4-8")
        assert name == "R_1_planner_us.anthropic-opus-4-8"

    def test_each_agent_constant_yields_distinct_uuid(self) -> None:
        agents = [
            AGENT_PLANNER,
            AGENT_PLAN_REVIEWER,
            AGENT_ADVISE,
            AGENT_LEARNINGS,
            AGENT_IMPLEMENTER,
            AGENT_PR_REVIEWER,
            AGENT_ADDRESS_REVIEW,
            AGENT_CI_DRIVER,
        ]
        uuids = {session_uuid("R", 1, a) for a in agents}
        assert len(uuids) == len(agents)

    def test_signature_does_not_accept_githash_kw(self) -> None:
        """Regression for #841: passing a githash kwarg must be a TypeError.

        The whole point of #841 is that the session id is a function of the
        artifact (repo, issue, agent) only — never a commit. If a caller
        sneaks ``githash=`` back into ``invoke_claude_with_session``, that
        kwarg would silently end up here and produce a different UUID per
        main-bump. Making the function refuse the kwarg outright keeps the
        invariant load-bearing rather than aspirational.

        Implementation note: the kwarg is invoked via ``**`` unpacking so
        static analyzers cannot resolve the offending parameter name back
        to the now-githash-free signatures — otherwise this very test
        would itself be flagged as "wrong arg name" on every PR scan.
        """
        bad_kwargs: dict[str, Any] = {"githash": "abc1234"}
        with pytest.raises(TypeError):
            session_uuid("R", 1, AGENT_PLANNER, **bad_kwargs)
        with pytest.raises(TypeError):
            session_name("R", 1, AGENT_PLANNER, **bad_kwargs)


@pytest.mark.requires_posix
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Creates a throwaway git repo via real `git init`/`git commit`; skipped on win32 (#742)",
)
class TestShortGithash:
    """``git rev-parse --short=7 HEAD`` wrapper with graceful failure."""

    def test_real_repo(self, tmp_path: Path) -> None:
        env = _git_test_env()
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "commit",
                "--allow-empty",
                "-m",
                "x",
                "--no-gpg-sign",
            ],
            check=True,
            env=env,
        )
        h = short_githash(tmp_path)
        assert len(h) == 7
        assert h != "unknown"
        assert all(c in "0123456789abcdef" for c in h)

    def test_missing_repo_returns_unknown(self, tmp_path: Path) -> None:
        assert short_githash(tmp_path) == "unknown"

    def test_ignores_outer_git_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GIT_DIR", str(Path.cwd() / ".git"))
        assert short_githash(tmp_path) == "unknown"


class TestSessionJsonlPath:
    """Location of Claude Code's per-session JSONL transcript."""

    def test_path_encoding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "Projects" / "Foo"
        target.mkdir(parents=True)
        p = session_jsonl_path("abc-uuid", target)
        assert p.name == "abc-uuid.jsonl"
        encoded = str(target.resolve()).replace("/", "-").replace(".", "-")
        assert encoded in str(p)
        assert p.parent.parent == tmp_path / ".claude" / "projects"

    def test_uuid_in_filename(self, tmp_path: Path) -> None:
        sid = session_uuid("R", 1, AGENT_IMPLEMENTER)
        p = session_jsonl_path(sid, tmp_path)
        assert p.name == f"{sid}.jsonl"

    def test_dot_prefixed_segment_encodes_dot_as_dash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Encode a `.worktrees` segment as `--worktrees-`, not `-.worktrees-`.

        Matches the Claude CLI's actual on-disk encoding. (#822)
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "build" / ".worktrees" / "issue-5451"
        target.mkdir(parents=True)
        p = session_jsonl_path("u", target)
        # `/.worktrees/` becomes `--worktrees-`, NOT `-.worktrees-`.
        assert "--worktrees-issue-5451" in str(p)
        assert "-.worktrees-" not in str(p)

    def test_multiple_dot_segments_all_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / ".venv" / ".tox" / "y"
        target.mkdir(parents=True)
        p = session_jsonl_path("u", target)
        # Both leading dots get rewritten.
        assert "." not in p.parent.name

    def test_mid_segment_dot_also_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rewrite mid-segment dots (e.g. `v1.2.3`) too.

        Matches the CLI's flat `.` -> `-` rule. Documented edge case, not a bug.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "release" / "v1.2.3" / "build"
        target.mkdir(parents=True)
        p = session_jsonl_path("u", target)
        assert "v1-2-3" in p.parent.name
        assert "v1.2.3" not in p.parent.name

    @pytest.mark.parametrize("created_in", ["repo_root", "worktree"])
    def test_registered_family_cwds_resolve_same_existing_transcript(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        created_in: str,
    ) -> None:
        """Repo-root and worktree callers resolve one existing transcript."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        repo_root = tmp_path / "owner-a" / "Repo"
        worktree = repo_root / "build" / ".worktrees" / "issue-2284"
        worktree.mkdir(parents=True)
        sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, "fable")

        source = repo_root if created_in == "repo_root" else worktree
        transcript = session_jsonl_path(sid, source)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}\n", encoding="utf-8")

        with patch(
            "hephaestus.automation.agent_config._registered_worktree_roots",
            return_value=(repo_root.resolve(), worktree.resolve()),
        ):
            assert resolve_session_jsonl_path(sid, repo_root) == transcript
            assert resolve_session_jsonl_path(sid, worktree) == transcript

    def test_same_slug_unrelated_checkout_transcript_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A same-key transcript from an unrelated checkout is not resumed."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        local = tmp_path / "owner-a" / "Repo"
        local_worktree = local / "build" / ".worktrees" / "issue-2284"
        unrelated = tmp_path / "owner-b" / "Repo"
        local_worktree.mkdir(parents=True)
        unrelated.mkdir(parents=True)

        sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, "fable")
        foreign_transcript = session_jsonl_path(sid, unrelated)
        foreign_transcript.parent.mkdir(parents=True, exist_ok=True)
        foreign_transcript.write_text("{}\n", encoding="utf-8")

        with patch(
            "hephaestus.automation.agent_config._registered_worktree_roots",
            return_value=(local.resolve(), local_worktree.resolve()),
        ):
            resolved = resolve_session_jsonl_path(sid, local_worktree)

        assert resolved == session_jsonl_path(sid, local_worktree)
        assert resolved != foreign_transcript
        assert not resolved.exists()

    def test_lossy_path_pair_cannot_resume_unregistered_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dotted/dashed checkouts isolate sessions but registered worktrees resume."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        dotted = tmp_path / "owner.a" / "Repo"
        dotted_worktree = dotted / "build" / ".worktrees" / "issue-2284"
        dashed = tmp_path / "owner-a" / "Repo"
        dotted_worktree.mkdir(parents=True)
        dashed.mkdir(parents=True)

        legacy_sid = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                session_name("Repo", 2284, AGENT_PLAN_REVIEWER, "fable"),
            )
        )
        assert session_jsonl_path(legacy_sid, dotted) == session_jsonl_path(legacy_sid, dashed)

        git_failure = subprocess.CalledProcessError(128, ["git"])
        with patch(
            "hephaestus.automation.agent_config.subprocess.run",
            side_effect=git_failure,
        ):
            dotted_sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, "fable", cwd=dotted)
            dashed_sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, "fable", cwd=dashed)

        dotted_path = session_jsonl_path(dotted_sid, dotted)
        dashed_path = session_jsonl_path(dashed_sid, dashed)
        assert dotted_path.parent == dashed_path.parent
        assert dotted_path != dashed_path
        assert dotted_sid != dashed_sid

        dashed_path.parent.mkdir(parents=True, exist_ok=True)
        dashed_path.write_text("{}\n", encoding="utf-8")
        with patch(
            "hephaestus.automation.agent_config._registered_worktree_roots",
            return_value=(dotted.resolve(),),
        ):
            resolved = resolve_session_jsonl_path(dotted_sid, dotted)

        assert resolved == dotted_path
        assert resolved != dashed_path
        assert not resolved.exists()

        dotted_path.parent.mkdir(parents=True, exist_ok=True)
        dotted_path.write_text("{}\n", encoding="utf-8")
        with patch(
            "hephaestus.automation.agent_config._registered_worktree_roots",
            return_value=(dotted.resolve(), dotted_worktree.resolve()),
        ):
            assert resolve_session_jsonl_path(dotted_sid, dotted_worktree) == dotted_path

    def test_worktree_discovery_parses_nul_output_and_scrubs_git_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery uses the explicit cwd and parses Git's NUL records."""
        repo_root = tmp_path / "Repo"
        worktree = tmp_path / "worktree"
        repo_root.mkdir()
        worktree.mkdir()
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign-tree"))
        output = (
            f"worktree {repo_root}\0HEAD deadbeef\0branch refs/heads/main\0\0"
            f"worktree {worktree}\0HEAD cafefood\0branch refs/heads/issue\0\0"
        )

        with patch(
            "hephaestus.automation.agent_config.subprocess.run",
            return_value=MagicMock(stdout=output),
        ) as run:
            roots = agent_config._registered_worktree_roots(worktree)

        assert roots == tuple(sorted((repo_root.resolve(), worktree.resolve()), key=str))
        argv = run.call_args.args[0]
        assert argv[:3] == ["git", "-C", str(worktree.resolve())]
        assert argv[-2:] == ["--porcelain", "-z"]
        kwargs = run.call_args.kwargs
        assert kwargs["timeout"] == 5
        assert "GIT_DIR" not in kwargs["env"]
        assert "GIT_WORK_TREE" not in kwargs["env"]

    def test_git_discovery_failure_falls_back_to_exact_cwd_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed Git probe preserves the old exact-cwd create behavior."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        cwd = tmp_path / "not-a-repository"
        cwd.mkdir()
        sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, "fable")

        with patch(
            "hephaestus.automation.agent_config.subprocess.run",
            side_effect=subprocess.CalledProcessError(128, ["git"]),
        ):
            assert resolve_session_jsonl_path(sid, cwd) == session_jsonl_path(sid, cwd)

    def test_historical_duplicates_choose_lexicographically_first_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Duplicate same-family transcripts have deterministic selection."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        repo_root = tmp_path / "owner-a" / "Repo"
        worktree_a = repo_root / "build" / ".worktrees" / "a"
        worktree_b = repo_root / "build" / ".worktrees" / "b"
        worktree_b.mkdir(parents=True)
        worktree_a.mkdir(parents=True)
        sid = session_uuid("Repo", 2284, AGENT_PLAN_REVIEWER, "fable")
        paths = [session_jsonl_path(sid, root) for root in (repo_root, worktree_a, worktree_b)]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

        with patch(
            "hephaestus.automation.agent_config._registered_worktree_roots",
            return_value=tuple(root.resolve() for root in (repo_root, worktree_a, worktree_b)),
        ):
            expected = min(paths, key=str)
            assert resolve_session_jsonl_path(sid, repo_root) == expected
            assert resolve_session_jsonl_path(sid, worktree_b) == expected


@pytest.mark.requires_posix
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Creates a throwaway git repo via real `git init`/`git commit`; skipped on win32 (#742)",
)
class TestCurrentTrunkGithash:
    """``current_trunk_githash`` uses explicit context or live rev-parse."""

    def test_explicit_trunk_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_TRUNK_GITHASH", "deadbee")
        # tmp_path is not a git repo; if the env var weren't honored we'd get
        # "unknown" from the fallback.
        assert current_trunk_githash(tmp_path, trunk_githash="deadbee") == "deadbee"

    def test_falls_back_to_short_githash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HEPH_TRUNK_GITHASH", raising=False)
        env = _git_test_env()
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "commit",
                "--allow-empty",
                "-m",
                "x",
                "--no-gpg-sign",
            ],
            check=True,
            env=env,
        )
        h = current_trunk_githash(tmp_path)
        assert len(h) == 7
        assert h != "unknown"

    def test_no_env_no_repo_returns_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HEPH_TRUNK_GITHASH", raising=False)
        assert current_trunk_githash(tmp_path) == "unknown"

    def test_empty_env_var_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty HEPH_TRUNK_GITHASH must fall back, not propagate ``""``."""
        monkeypatch.setenv("HEPH_TRUNK_GITHASH", "")
        assert current_trunk_githash(tmp_path) == "unknown"
