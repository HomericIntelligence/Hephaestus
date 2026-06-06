"""Regression: tidy agent-prompt template must not hardcode gh pr merge method."""
import re

from hephaestus.github import tidy


def test_agent_prompt_does_not_hardcode_merge_method() -> None:
    # Since the template is created in _make_agent_prompt, we need to call it with dummy params
    # and verify the output doesn't contain hardcoded merge flags
    import inspect
    
    # Get the source of _make_agent_prompt
    source = inspect.getsource(tidy._make_agent_prompt)
    
    # Check for hardcoded merge flags in the source
    assert not re.search(r"--auto\s+--(rebase|squash|merge)\b", source), (
        "tidy._make_agent_prompt still hardcodes a merge method; "
        "use choose_merge_flag instead."
    )
    assert "choose_merge_flag" in source
