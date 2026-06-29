"""Regression: tidy agent-prompt template must not arm auto-merge."""

from hephaestus.github import tidy


def test_agent_prompt_does_not_arm_auto_merge() -> None:
    """Verify tidy agent prompt leaves PR policy auto-merge handling out of scope."""
    import inspect

    # Get the source of _make_agent_prompt
    source = inspect.getsource(tidy._make_agent_prompt)

    assert "pr merge --auto" not in source
    assert "choose_merge_flag" not in source
    assert "state:implementation-go" not in source
