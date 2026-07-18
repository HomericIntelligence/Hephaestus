"""Follow-up prompt: identify in-scope follow-ups discovered during implementation."""

from ._shared import get_terse_output_directive
from .catalog import PromptCatalog

def get_follow_up_prompt(issue_number: int) -> str:
    """Get the follow-up prompt for identifying future work.

    Args:
        issue_number: GitHub issue number

    Returns:
        Formatted follow-up prompt

    """
    return PromptCatalog.current().render(
        "follow_up/follow_up.j2",
        issue_number=issue_number,
        terse_output_directive=get_terse_output_directive(),
    )
