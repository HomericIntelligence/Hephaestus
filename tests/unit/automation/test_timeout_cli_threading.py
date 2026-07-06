"""Tests proving CLI timeout flags thread through to Options objects.

Each new ``add_*_timeout_arg`` helper is exercised in isolation, and the
Options models are verified to hold the expected fields with the correct
defaults.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hephaestus.automation.agent_config import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_CI_POLL_MAX_WAIT,
    DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
)
from hephaestus.automation.models import (
    AddressReviewOptions,
    CIDriverOptions,
    ImplementerOptions,
    PlannerOptions,
    PlanReviewerOptions,
    ReviewerOptions,
)
from hephaestus.cli.utils import (
    add_advise_timeout_arg,
    add_agent_timeout_arg,
    add_follow_up_timeout_arg,
    add_git_message_timeout_arg,
    add_learn_timeout_arg,
    add_poll_max_wait_arg,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _fresh_parser() -> argparse.ArgumentParser:
    """Return a plain ArgumentParser for flag isolation tests."""
    return argparse.ArgumentParser()


# ---------------------------------------------------------------------------
# add_agent_timeout_arg
# ---------------------------------------------------------------------------


class TestAddAgentTimeoutArg:
    """Tests for add_agent_timeout_arg helper."""

    def test_parses_integer_value(self) -> None:
        """--agent-timeout N is stored as int N on args.agent_timeout."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        args = parser.parse_args(["--agent-timeout", "3600"])
        assert args.agent_timeout == 3600
        assert isinstance(args.agent_timeout, int)

    def test_default_is_none_when_not_provided(self) -> None:
        """Omitting --agent-timeout leaves args.agent_timeout as None."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.agent_timeout is None

    def test_custom_flag_and_dest(self) -> None:
        """Custom flag and dest are honoured by the helper."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser, flag="--planner-timeout", dest="planner_timeout")
        args = parser.parse_args(["--planner-timeout", "100"])
        assert args.planner_timeout == 100
        assert not hasattr(args, "agent_timeout")

    def test_custom_default_doc_does_not_change_parse_default(self) -> None:
        """default_doc is display-only; parse default stays None."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser, default_doc=999)
        args = parser.parse_args([])
        assert args.agent_timeout is None

    def test_zero_is_accepted(self) -> None:
        """Zero is a valid integer value (no lower-bound guard in this helper)."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        args = parser.parse_args(["--agent-timeout", "0"])
        assert args.agent_timeout == 0

    def test_non_integer_exits_with_error(self) -> None:
        """A non-integer value triggers argparse error (exits 2)."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--agent-timeout", "not-a-number"])
        assert exc.value.code == 2

    def test_help_extra_appears_in_help_text(self) -> None:
        """help_extra text is incorporated into the flag's help string."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser, help_extra="Overrides the default.")
        help_text = parser.format_help()
        assert "Overrides the default." in help_text

    def test_metavar_is_seconds(self) -> None:
        """Metavar shown in help is SECONDS."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        help_text = parser.format_help()
        assert "SECONDS" in help_text


# ---------------------------------------------------------------------------
# add_advise_timeout_arg
# ---------------------------------------------------------------------------


class TestAddAdviseTimeoutArg:
    """Tests for add_advise_timeout_arg helper."""

    def test_parses_integer_value(self) -> None:
        """--advise-timeout N is stored as int N on args.advise_timeout."""
        parser = _fresh_parser()
        add_advise_timeout_arg(parser)
        args = parser.parse_args(["--advise-timeout", "1800"])
        assert args.advise_timeout == 1800
        assert isinstance(args.advise_timeout, int)

    def test_default_is_none_when_not_provided(self) -> None:
        """Omitting --advise-timeout leaves args.advise_timeout as None."""
        parser = _fresh_parser()
        add_advise_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.advise_timeout is None

    def test_dest_is_advise_timeout(self) -> None:
        """The destination attribute is named advise_timeout."""
        parser = _fresh_parser()
        add_advise_timeout_arg(parser)
        args = parser.parse_args(["--advise-timeout", "42"])
        assert hasattr(args, "advise_timeout")

    def test_non_integer_exits_with_error(self) -> None:
        """A non-integer value triggers argparse error (exits 2)."""
        parser = _fresh_parser()
        add_advise_timeout_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--advise-timeout", "abc"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# add_git_message_timeout_arg
# ---------------------------------------------------------------------------


class TestAddGitMessageTimeoutArg:
    """Tests for add_git_message_timeout_arg helper."""

    def test_parses_integer_value(self) -> None:
        """--git-message-timeout N is stored as int N on args.git_message_timeout."""
        parser = _fresh_parser()
        add_git_message_timeout_arg(parser)
        args = parser.parse_args(["--git-message-timeout", "300"])
        assert args.git_message_timeout == 300
        assert isinstance(args.git_message_timeout, int)

    def test_default_is_none_when_not_provided(self) -> None:
        """Omitting --git-message-timeout leaves args.git_message_timeout as None."""
        parser = _fresh_parser()
        add_git_message_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.git_message_timeout is None

    def test_dest_is_git_message_timeout(self) -> None:
        """The destination attribute is named git_message_timeout."""
        parser = _fresh_parser()
        add_git_message_timeout_arg(parser)
        args = parser.parse_args(["--git-message-timeout", "60"])
        assert hasattr(args, "git_message_timeout")

    def test_non_integer_exits_with_error(self) -> None:
        """A non-integer value triggers argparse error (exits 2)."""
        parser = _fresh_parser()
        add_git_message_timeout_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--git-message-timeout", "3.5"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# add_learn_timeout_arg
# ---------------------------------------------------------------------------


class TestAddLearnTimeoutArg:
    """Tests for add_learn_timeout_arg helper."""

    def test_parses_integer_value(self) -> None:
        """--learn-timeout N is stored as int N on args.learn_timeout."""
        parser = _fresh_parser()
        add_learn_timeout_arg(parser)
        args = parser.parse_args(["--learn-timeout", "7200"])
        assert args.learn_timeout == 7200
        assert isinstance(args.learn_timeout, int)

    def test_default_is_none_when_not_provided(self) -> None:
        """Omitting --learn-timeout leaves args.learn_timeout as None."""
        parser = _fresh_parser()
        add_learn_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.learn_timeout is None

    def test_dest_is_learn_timeout(self) -> None:
        """The destination attribute is named learn_timeout."""
        parser = _fresh_parser()
        add_learn_timeout_arg(parser)
        args = parser.parse_args(["--learn-timeout", "500"])
        assert hasattr(args, "learn_timeout")

    def test_non_integer_exits_with_error(self) -> None:
        """A non-integer value triggers argparse error (exits 2)."""
        parser = _fresh_parser()
        add_learn_timeout_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--learn-timeout", "inf"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# add_follow_up_timeout_arg
# ---------------------------------------------------------------------------


class TestAddFollowUpTimeoutArg:
    """Tests for add_follow_up_timeout_arg helper."""

    def test_parses_integer_value(self) -> None:
        """--follow-up-timeout N is stored as int N on args.follow_up_timeout."""
        parser = _fresh_parser()
        add_follow_up_timeout_arg(parser)
        args = parser.parse_args(["--follow-up-timeout", "4800"])
        assert args.follow_up_timeout == 4800
        assert isinstance(args.follow_up_timeout, int)

    def test_default_is_none_when_not_provided(self) -> None:
        """Omitting --follow-up-timeout leaves args.follow_up_timeout as None."""
        parser = _fresh_parser()
        add_follow_up_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.follow_up_timeout is None

    def test_dest_is_follow_up_timeout(self) -> None:
        """The destination attribute is named follow_up_timeout."""
        parser = _fresh_parser()
        add_follow_up_timeout_arg(parser)
        args = parser.parse_args(["--follow-up-timeout", "100"])
        assert hasattr(args, "follow_up_timeout")

    def test_non_integer_exits_with_error(self) -> None:
        """A non-integer value triggers argparse error (exits 2)."""
        parser = _fresh_parser()
        add_follow_up_timeout_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--follow-up-timeout", "fast"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# add_poll_max_wait_arg
# ---------------------------------------------------------------------------


class TestAddPollMaxWaitArg:
    """Tests for add_poll_max_wait_arg helper."""

    def test_parses_integer_value(self) -> None:
        """--poll-max-wait N is stored as int N on args.poll_max_wait."""
        parser = _fresh_parser()
        add_poll_max_wait_arg(parser)
        args = parser.parse_args(["--poll-max-wait", "600"])
        assert args.poll_max_wait == 600
        assert isinstance(args.poll_max_wait, int)

    def test_default_is_none_when_not_provided(self) -> None:
        """Omitting --poll-max-wait leaves args.poll_max_wait as None."""
        parser = _fresh_parser()
        add_poll_max_wait_arg(parser)
        args = parser.parse_args([])
        assert args.poll_max_wait is None

    def test_dest_is_poll_max_wait(self) -> None:
        """The destination attribute is named poll_max_wait."""
        parser = _fresh_parser()
        add_poll_max_wait_arg(parser)
        args = parser.parse_args(["--poll-max-wait", "1200"])
        assert hasattr(args, "poll_max_wait")

    def test_non_integer_exits_with_error(self) -> None:
        """A non-integer value triggers argparse error (exits 2)."""
        parser = _fresh_parser()
        add_poll_max_wait_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--poll-max-wait", "long"])
        assert exc.value.code == 2

    def test_large_value_is_accepted(self) -> None:
        """A large integer (e.g. 86400) is accepted without complaint."""
        parser = _fresh_parser()
        add_poll_max_wait_arg(parser)
        args = parser.parse_args(["--poll-max-wait", "86400"])
        assert args.poll_max_wait == 86400


# ---------------------------------------------------------------------------
# All helpers can co-exist on the same parser
# ---------------------------------------------------------------------------


class TestCombinedFlags:
    """All timeout helpers can be added to a single parser without collisions."""

    def _build_full_parser(self) -> argparse.ArgumentParser:
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        add_advise_timeout_arg(parser)
        add_git_message_timeout_arg(parser)
        add_learn_timeout_arg(parser)
        add_follow_up_timeout_arg(parser)
        add_poll_max_wait_arg(parser)
        return parser

    def test_all_flags_parse_together(self) -> None:
        """All six timeout flags can be specified in a single parse_args call."""
        parser = self._build_full_parser()
        args = parser.parse_args(
            [
                "--agent-timeout",
                "7200",
                "--advise-timeout",
                "3600",
                "--git-message-timeout",
                "300",
                "--learn-timeout",
                "1800",
                "--follow-up-timeout",
                "900",
                "--poll-max-wait",
                "600",
            ]
        )
        assert args.agent_timeout == 7200
        assert args.advise_timeout == 3600
        assert args.git_message_timeout == 300
        assert args.learn_timeout == 1800
        assert args.follow_up_timeout == 900
        assert args.poll_max_wait == 600

    def test_all_defaults_are_none_when_flags_omitted(self) -> None:
        """When all flags are absent each dest defaults to None."""
        parser = self._build_full_parser()
        args = parser.parse_args([])
        assert args.agent_timeout is None
        assert args.advise_timeout is None
        assert args.git_message_timeout is None
        assert args.learn_timeout is None
        assert args.follow_up_timeout is None
        assert args.poll_max_wait is None


# ---------------------------------------------------------------------------
# Constant values
# ---------------------------------------------------------------------------


class TestDefaultConstants:
    """Numeric constant values are documented and stable."""

    def test_default_agent_timeout_is_7200(self) -> None:
        """DEFAULT_AGENT_TIMEOUT equals 7200 seconds (two hours)."""
        assert DEFAULT_AGENT_TIMEOUT == 7200

    def test_default_git_message_agent_timeout_is_1200(self) -> None:
        """DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT equals 1200 seconds."""
        assert DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT == 1200

    def test_default_ci_poll_max_wait_is_1200(self) -> None:
        """DEFAULT_CI_POLL_MAX_WAIT equals 1200 seconds."""
        assert DEFAULT_CI_POLL_MAX_WAIT == 1200


# ---------------------------------------------------------------------------
# PlannerOptions timeout fields
# ---------------------------------------------------------------------------


class TestPlannerOptionsTimeoutFields:
    """PlannerOptions exposes agent/advise/git-message timeout fields."""

    def test_has_agent_timeout_field(self) -> None:
        """PlannerOptions declares an agent_timeout field."""
        assert "agent_timeout" in PlannerOptions.model_fields

    def test_has_advise_timeout_field(self) -> None:
        """PlannerOptions declares an advise_timeout field."""
        assert "advise_timeout" in PlannerOptions.model_fields

    def test_has_git_message_timeout_field(self) -> None:
        """PlannerOptions declares a git_message_timeout field."""
        assert "git_message_timeout" in PlannerOptions.model_fields

    def test_agent_timeout_default(self) -> None:
        """PlannerOptions.agent_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = PlannerOptions(issues=[1])
        assert opts.agent_timeout == DEFAULT_AGENT_TIMEOUT

    def test_advise_timeout_default(self) -> None:
        """PlannerOptions.advise_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = PlannerOptions(issues=[1])
        assert opts.advise_timeout == DEFAULT_AGENT_TIMEOUT

    def test_git_message_timeout_default(self) -> None:
        """PlannerOptions.git_message_timeout defaults to DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT."""
        opts = PlannerOptions(issues=[1])
        assert opts.git_message_timeout == DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT

    def test_agent_timeout_override(self) -> None:
        """PlannerOptions.agent_timeout can be overridden at construction time."""
        opts = PlannerOptions(issues=[1], agent_timeout=999)
        assert opts.agent_timeout == 999

    def test_advise_timeout_override(self) -> None:
        """PlannerOptions.advise_timeout can be overridden at construction time."""
        opts = PlannerOptions(issues=[1], advise_timeout=500)
        assert opts.advise_timeout == 500

    def test_git_message_timeout_override(self) -> None:
        """PlannerOptions.git_message_timeout can be overridden at construction time."""
        opts = PlannerOptions(issues=[1], git_message_timeout=60)
        assert opts.git_message_timeout == 60


# ---------------------------------------------------------------------------
# ImplementerOptions timeout fields
# ---------------------------------------------------------------------------


class TestImplementerOptionsTimeoutFields:
    """ImplementerOptions exposes agent/advise/git-message/learn/follow-up timeout fields."""

    def test_has_agent_timeout_field(self) -> None:
        """ImplementerOptions declares an agent_timeout field."""
        assert "agent_timeout" in ImplementerOptions.model_fields

    def test_has_advise_timeout_field(self) -> None:
        """ImplementerOptions declares an advise_timeout field."""
        assert "advise_timeout" in ImplementerOptions.model_fields

    def test_has_git_message_timeout_field(self) -> None:
        """ImplementerOptions declares a git_message_timeout field."""
        assert "git_message_timeout" in ImplementerOptions.model_fields

    def test_has_learn_timeout_field(self) -> None:
        """ImplementerOptions declares a learn_timeout field."""
        assert "learn_timeout" in ImplementerOptions.model_fields

    def test_has_follow_up_timeout_field(self) -> None:
        """ImplementerOptions declares a follow_up_timeout field."""
        assert "follow_up_timeout" in ImplementerOptions.model_fields

    def test_agent_timeout_default(self) -> None:
        """ImplementerOptions.agent_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = ImplementerOptions()
        assert opts.agent_timeout == DEFAULT_AGENT_TIMEOUT

    def test_advise_timeout_default(self) -> None:
        """ImplementerOptions.advise_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = ImplementerOptions()
        assert opts.advise_timeout == DEFAULT_AGENT_TIMEOUT

    def test_git_message_timeout_default(self) -> None:
        """ImplementerOptions.git_message_timeout defaults to DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT."""
        opts = ImplementerOptions()
        assert opts.git_message_timeout == DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT

    def test_learn_timeout_default(self) -> None:
        """ImplementerOptions.learn_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = ImplementerOptions()
        assert opts.learn_timeout == DEFAULT_AGENT_TIMEOUT

    def test_follow_up_timeout_default(self) -> None:
        """ImplementerOptions.follow_up_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = ImplementerOptions()
        assert opts.follow_up_timeout == DEFAULT_AGENT_TIMEOUT

    def test_all_timeout_overrides(self) -> None:
        """All five timeout fields accept explicit override values."""
        opts = ImplementerOptions(
            agent_timeout=1,
            advise_timeout=2,
            git_message_timeout=3,
            learn_timeout=4,
            follow_up_timeout=5,
        )
        assert opts.agent_timeout == 1
        assert opts.advise_timeout == 2
        assert opts.git_message_timeout == 3
        assert opts.learn_timeout == 4
        assert opts.follow_up_timeout == 5


# ---------------------------------------------------------------------------
# CIDriverOptions timeout fields
# ---------------------------------------------------------------------------


class TestCIDriverOptionsTimeoutFields:
    """CIDriverOptions exposes agent/advise/learn timeout fields and poll_max_wait."""

    def test_has_agent_timeout_field(self) -> None:
        """CIDriverOptions declares an agent_timeout field."""
        assert "agent_timeout" in CIDriverOptions.model_fields

    def test_has_advise_timeout_field(self) -> None:
        """CIDriverOptions declares an advise_timeout field."""
        assert "advise_timeout" in CIDriverOptions.model_fields

    def test_has_learn_timeout_field(self) -> None:
        """CIDriverOptions declares a learn_timeout field."""
        assert "learn_timeout" in CIDriverOptions.model_fields

    def test_has_poll_max_wait_field(self) -> None:
        """CIDriverOptions declares a poll_max_wait field."""
        assert "poll_max_wait" in CIDriverOptions.model_fields

    def test_agent_timeout_default(self) -> None:
        """CIDriverOptions.agent_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = CIDriverOptions()
        assert opts.agent_timeout == DEFAULT_AGENT_TIMEOUT

    def test_advise_timeout_default(self) -> None:
        """CIDriverOptions.advise_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = CIDriverOptions()
        assert opts.advise_timeout == DEFAULT_AGENT_TIMEOUT

    def test_learn_timeout_default(self) -> None:
        """CIDriverOptions.learn_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = CIDriverOptions()
        assert opts.learn_timeout == DEFAULT_AGENT_TIMEOUT

    def test_poll_max_wait_default(self) -> None:
        """CIDriverOptions.poll_max_wait defaults to DEFAULT_CI_POLL_MAX_WAIT."""
        opts = CIDriverOptions()
        assert opts.poll_max_wait == DEFAULT_CI_POLL_MAX_WAIT

    def test_all_timeout_overrides(self) -> None:
        """All four timeout-related fields accept explicit override values."""
        opts = CIDriverOptions(
            agent_timeout=10,
            advise_timeout=20,
            learn_timeout=30,
            poll_max_wait=40,
        )
        assert opts.agent_timeout == 10
        assert opts.advise_timeout == 20
        assert opts.learn_timeout == 30
        assert opts.poll_max_wait == 40


# ---------------------------------------------------------------------------
# ReviewerOptions timeout fields
# ---------------------------------------------------------------------------


class TestReviewerOptionsTimeoutFields:
    """ReviewerOptions exposes an agent_timeout field."""

    def test_has_agent_timeout_field(self) -> None:
        """ReviewerOptions declares an agent_timeout field."""
        assert "agent_timeout" in ReviewerOptions.model_fields

    def test_has_no_learn_timeout_field(self) -> None:
        """ReviewerOptions does NOT expose learn_timeout (no consumer threads it)."""
        assert "learn_timeout" not in ReviewerOptions.model_fields

    def test_agent_timeout_default(self) -> None:
        """ReviewerOptions.agent_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = ReviewerOptions()
        assert opts.agent_timeout == DEFAULT_AGENT_TIMEOUT

    def test_timeout_override(self) -> None:
        """agent_timeout accepts an explicit override value."""
        opts = ReviewerOptions(agent_timeout=111)
        assert opts.agent_timeout == 111


# ---------------------------------------------------------------------------
# PlanReviewerOptions timeout fields
# ---------------------------------------------------------------------------


class TestPlanReviewerOptionsTimeoutFields:
    """PlanReviewerOptions exposes an agent_timeout field."""

    def test_has_agent_timeout_field(self) -> None:
        """PlanReviewerOptions declares an agent_timeout field."""
        assert "agent_timeout" in PlanReviewerOptions.model_fields

    def test_agent_timeout_default(self) -> None:
        """PlanReviewerOptions.agent_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = PlanReviewerOptions()
        assert opts.agent_timeout == DEFAULT_AGENT_TIMEOUT

    def test_agent_timeout_override(self) -> None:
        """PlanReviewerOptions.agent_timeout can be overridden at construction time."""
        opts = PlanReviewerOptions(agent_timeout=5000)
        assert opts.agent_timeout == 5000


# ---------------------------------------------------------------------------
# AddressReviewOptions timeout fields
# ---------------------------------------------------------------------------


class TestAddressReviewOptionsTimeoutFields:
    """AddressReviewOptions exposes agent_timeout and advise_timeout fields."""

    def test_has_agent_timeout_field(self) -> None:
        """AddressReviewOptions declares an agent_timeout field."""
        assert "agent_timeout" in AddressReviewOptions.model_fields

    def test_has_advise_timeout_field(self) -> None:
        """AddressReviewOptions declares an advise_timeout field."""
        assert "advise_timeout" in AddressReviewOptions.model_fields

    def test_agent_timeout_default(self) -> None:
        """AddressReviewOptions.agent_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = AddressReviewOptions()
        assert opts.agent_timeout == DEFAULT_AGENT_TIMEOUT

    def test_advise_timeout_default(self) -> None:
        """AddressReviewOptions.advise_timeout defaults to DEFAULT_AGENT_TIMEOUT."""
        opts = AddressReviewOptions()
        assert opts.advise_timeout == DEFAULT_AGENT_TIMEOUT

    def test_timeout_overrides(self) -> None:
        """Timeout fields accept explicit override values."""
        opts = AddressReviewOptions(agent_timeout=333, advise_timeout=444)
        assert opts.agent_timeout == 333
        assert opts.advise_timeout == 444


# ---------------------------------------------------------------------------
# End-to-end threading: CLI flag values reach the downstream leaf kwarg
# ---------------------------------------------------------------------------


class TestImplementerMainTimeoutThreading:
    """Prove the thin implementer main() accepts the historical timeout flags.

    Since the epic #1809 pipeline conversion (#1821) ``implementer.main()`` is a
    thin wrapper that seeds ``--issues`` into the ``(implementation, pr_review)``
    pipeline scope and dispatches to ``run_pipeline``. The per-phase timeout
    flags (``--git-message-timeout`` / ``--agent-timeout`` / ``--learn-timeout``)
    are retained on the CLI surface for backward compatibility with pinned
    callers and the loop runner's child-phase argv; these tests pin that they
    still parse and that main() dispatches cleanly. Per-agent-job timeout
    behavior is exercised by the pipeline stage / coordinator tests.
    """

    @pytest.mark.parametrize(
        "flag",
        ["--git-message-timeout", "--agent-timeout", "--learn-timeout", "--advise-timeout"],
    )
    def test_timeout_flag_parses_and_main_dispatches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str
    ) -> None:
        """A historical per-phase timeout flag parses and main() reaches run_pipeline."""
        import sys
        from unittest.mock import patch

        from hephaestus.automation import implementer

        monkeypatch.setattr(
            sys,
            "argv",
            ["impl", "--issues", "42", "--dry-run", "--no-ui", "--agent", "claude", flag, "1234"],
        )

        with (
            patch.object(implementer, "_resolve_repo", return_value=("acme", "widget")),
            patch.object(implementer, "get_repo_root", return_value=tmp_path),
            patch.object(implementer, "resolve_agent", return_value="claude"),
            patch(
                "hephaestus.automation.pipeline.coordinator.run_pipeline",
                return_value=0,
            ) as mock_run,
        ):
            rc = implementer.main()

        assert rc == 0
        mock_run.assert_called_once()
        config = mock_run.call_args.args[0]
        assert config.issues == [42]


class TestPostMergeLearnTimeoutThreading:
    """Prove CIDriverOptions.learn_timeout reaches agent call in run_drive_green_learnings."""

    def test_learn_timeout_reaches_run_agent_session(self, tmp_path: Path) -> None:
        """CIDriverOptions.learn_timeout reaches the timeout= kwarg in run_agent_session.

        PostMergeProcessor.run_drive_green_learnings reads options.learn_timeout from
        the CIDriverOptions provider and passes it as timeout= to the agent runner.
        """
        from unittest.mock import MagicMock, patch

        from hephaestus.automation.models import CIDriverOptions
        from hephaestus.automation.post_merge_processor import PostMergeProcessor

        sentinel = 3399

        captured: dict[str, object] = {}

        def fake_run_agent_session(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            result = MagicMock()
            result.stdout = ""
            return result

        opts = CIDriverOptions(learn_timeout=sentinel, agent="codex")
        saved: dict[int, dict] = {}

        proc = PostMergeProcessor(
            options_provider=lambda: opts,
            repo_root_provider=lambda: tmp_path,
            # Let the worktree lookup raise so the fallback to repo_root is taken.
            get_worktree_path=lambda i, pr: (_ for _ in ()).throw(RuntimeError("gone")),
            load_arming_state=lambda i: saved.get(i),
            save_arming_state=lambda i, r: saved.__setitem__(i, r),
        )

        _mod = "hephaestus.automation.post_merge_processor"
        with (
            patch(f"{_mod}.uses_direct_agent_runner", return_value=True),
            patch(f"{_mod}.run_agent_session", side_effect=fake_run_agent_session),
            patch(f"{_mod}.direct_agent_model", return_value="test-model"),
            patch(f"{_mod}.build_learn_prompt", return_value="test prompt"),
            patch(f"{_mod}.get_repo_slug", return_value="test/repo"),
        ):
            result = proc.run_drive_green_learnings(issue_number=1, pr_number=2)

        assert result is True
        assert captured.get("timeout") == sentinel, (
            f"expected timeout={sentinel}, got {captured.get('timeout')}"
        )
