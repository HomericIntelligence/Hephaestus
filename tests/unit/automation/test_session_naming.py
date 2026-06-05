"""Unit tests for deterministic session naming."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

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
        assert session_name("ProjectScylla", 1944, AGENT_PLANNER) == "ProjectScylla_1944_planner"

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
        a = session_uuid("ProjectScylla", 1944, AGENT_PLANNER)
        b = session_uuid("ProjectScylla", 1944, AGENT_PLANNER)
        assert a == b

    def test_returns_valid_uuid(self) -> None:
        sid = session_uuid("ProjectScylla", 1944, AGENT_PLANNER)
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
        bad_kwargs = {"githash": "abc1234"}
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


@pytest.mark.requires_posix
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Creates a throwaway git repo via real `git init`/`git commit`; skipped on win32 (#742)",
)
class TestCurrentTrunkGithash:
    """``current_trunk_githash`` reads env or falls back to live rev-parse."""

    def test_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_TRUNK_GITHASH", "deadbee")
        # tmp_path is not a git repo; if the env var weren't honored we'd get
        # "unknown" from the fallback.
        assert current_trunk_githash(tmp_path) == "deadbee"

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
