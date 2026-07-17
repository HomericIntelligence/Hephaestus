"""Backward-compatibility shim. Canonical impl: agent_config (#1441)."""

from hephaestus.automation.agent_config import (
    AGENT_ADDRESS_REVIEW as AGENT_ADDRESS_REVIEW,
    AGENT_ADVISE as AGENT_ADVISE,
    AGENT_CI_DRIVER as AGENT_CI_DRIVER,
    AGENT_COMMENT_CLASSIFIER as AGENT_COMMENT_CLASSIFIER,
    AGENT_COMMIT_MESSAGE as AGENT_COMMIT_MESSAGE,
    AGENT_IMPLEMENTER as AGENT_IMPLEMENTER,
    AGENT_LEARNINGS as AGENT_LEARNINGS,
    AGENT_PLAN_REVIEWER as AGENT_PLAN_REVIEWER,
    AGENT_PLANNER as AGENT_PLANNER,
    AGENT_PR_MESSAGE as AGENT_PR_MESSAGE,
    AGENT_PR_REVIEWER as AGENT_PR_REVIEWER,
    AGENT_STRICT_REVIEW as AGENT_STRICT_REVIEW,
    current_trunk_githash as current_trunk_githash,
    issue_auto_impl_branch_name as issue_auto_impl_branch_name,
    reviewer_agent as reviewer_agent,
    session_jsonl_path as session_jsonl_path,
    session_name as session_name,
    session_uuid as session_uuid,
    short_githash as short_githash,
    strict_review_agent as strict_review_agent,
)
