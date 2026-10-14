"""Test retained library timeout argument helpers."""

from __future__ import annotations

import argparse
from collections.abc import Callable

import pytest

from hephaestus.automation.agent_config import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
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

    def test_default_is_established_budget_when_not_provided(self) -> None:
        """Omitting --agent-timeout uses the established 7200-second budget."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.agent_timeout == 7200

    def test_custom_flag_and_dest(self) -> None:
        """Custom flag and dest are honoured by the helper."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser, flag="--planner-timeout", dest="planner_timeout")
        args = parser.parse_args(["--planner-timeout", "100"])
        assert args.planner_timeout == 100
        assert not hasattr(args, "agent_timeout")

    def test_custom_default_changes_parse_default(self) -> None:
        """The typed default is both documented and returned by argparse."""
        parser = _fresh_parser()
        add_agent_timeout_arg(parser, default=999)
        args = parser.parse_args([])
        assert args.agent_timeout == 999

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

    def test_default_is_established_budget_when_not_provided(self) -> None:
        """Omitting --advise-timeout uses the established 7200-second budget."""
        parser = _fresh_parser()
        add_advise_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.advise_timeout == 7200

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

    def test_default_is_established_budget_when_not_provided(self) -> None:
        """Omitting --git-message-timeout uses the established 1200-second budget."""
        parser = _fresh_parser()
        add_git_message_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.git_message_timeout == 1200

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

    def test_default_is_established_budget_when_not_provided(self) -> None:
        """Omitting --learn-timeout uses the established 1200-second budget."""
        parser = _fresh_parser()
        add_learn_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.learn_timeout == 1200

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

    def test_default_is_established_budget_when_not_provided(self) -> None:
        """Omitting --follow-up-timeout uses the established 7200-second budget."""
        parser = _fresh_parser()
        add_follow_up_timeout_arg(parser)
        args = parser.parse_args([])
        assert args.follow_up_timeout == 7200

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

    def test_default_is_established_budget_when_not_provided(self) -> None:
        """Omitting --poll-max-wait uses the established 1200-second budget."""
        parser = _fresh_parser()
        add_poll_max_wait_arg(parser)
        args = parser.parse_args([])
        assert args.poll_max_wait == 1200

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

    def test_all_defaults_are_established_budgets_when_flags_omitted(self) -> None:
        """Every omitted flag resolves to its established positive budget."""
        parser = self._build_full_parser()
        args = parser.parse_args([])
        assert args.agent_timeout == 7200
        assert args.advise_timeout == 7200
        assert args.git_message_timeout == 1200
        assert args.learn_timeout == 1200
        assert args.follow_up_timeout == 7200
        assert args.poll_max_wait == 1200


_TIMEOUT_ARGUMENTS: tuple[tuple[Callable[[argparse.ArgumentParser], None], str], ...] = (
    (add_agent_timeout_arg, "--agent-timeout"),
    (add_advise_timeout_arg, "--advise-timeout"),
    (add_poll_max_wait_arg, "--poll-max-wait"),
    (add_git_message_timeout_arg, "--git-message-timeout"),
    (add_learn_timeout_arg, "--learn-timeout"),
    (add_follow_up_timeout_arg, "--follow-up-timeout"),
)


@pytest.mark.parametrize(("add_timeout_arg", "flag"), _TIMEOUT_ARGUMENTS)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_timeout_is_rejected(
    add_timeout_arg: Callable[[argparse.ArgumentParser], None], flag: str, value: str
) -> None:
    """Shared timeout flags reject zero and negative values consistently."""
    parser = _fresh_parser()
    add_timeout_arg(parser)

    with pytest.raises(SystemExit) as exc:
        parser.parse_args([flag, value])

    assert exc.value.code == 2
    help_text = " ".join(parser.format_help().split())
    assert "positive integer" in help_text
    assert "zero does not disable" in help_text


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
