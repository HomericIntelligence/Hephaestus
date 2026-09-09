"""Backward-compatibility shim. Canonical impl: agent_config (#1441)."""

from hephaestus.automation.agent_config import (
    AGENT_IMPL_TIMEOUT as AGENT_IMPL_TIMEOUT,
    AGENT_LEARN_TIMEOUT as AGENT_LEARN_TIMEOUT,
    AGENT_PLAN_TIMEOUT as AGENT_PLAN_TIMEOUT,
    AGENT_REVIEW_TIMEOUT as AGENT_REVIEW_TIMEOUT,
    DEFAULT_AGENT_TIMEOUT as DEFAULT_AGENT_TIMEOUT,
    DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT as DEFAULT_GIT_MESSAGE_AGENT_TIMEOUT,
    DEFAULT_THROUGHPUT_TIMEOUT as DEFAULT_THROUGHPUT_TIMEOUT,
    advise_claude_timeout as advise_claude_timeout,
    agent_default_timeout as agent_default_timeout,
    gh_cli_timeout as gh_cli_timeout,
    git_message_agent_timeout as git_message_agent_timeout,
    implementer_claude_timeout as implementer_claude_timeout,
    learn_claude_timeout as learn_claude_timeout,
    plan_reviewer_claude_timeout as plan_reviewer_claude_timeout,
    planner_claude_timeout as planner_claude_timeout,
    pr_reviewer_claude_timeout as pr_reviewer_claude_timeout,
)
