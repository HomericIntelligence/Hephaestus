"""Regression: tidy agent-prompt template must not hardcode gh pr merge method."""

import re

from hephaestus.github import tidy


def test_agent_prompt_does_not_hardcode_merge_method() -> None:
    """Verify tidy agent prompt does not hardcode gh pr merge method."""
    import inspect

    # Get the source of _make_agent_prompt
    source = inspect.getsource(tidy._make_agent_prompt)

    # Check for hardcoded merge flags in the source
    assert not re.search(r"--auto\s+--(rebase|squash|merge)\b", source), (
        "tidy._make_agent_prompt still hardcodes a merge method; use choose_merge_flag instead."
    )
    assert "choose_merge_flag" in source
