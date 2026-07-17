"""Advise-phase prompt: select team knowledge to inject into automation prompts."""

from collections.abc import Callable

from hephaestus.agents.runtime import uses_direct_agent_runner

from ._shared import _relativize_path, get_terse_output_directive
from .catalog import PromptCatalog


def get_advise_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    marketplace_path: str,
    repo_root: str | None = None,
    marketplace_json: str = '{"plugins": []}',
) -> str:
    """Get the advise prompt for searching team knowledge.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title
        issue_body: Issue body/description
        marketplace_path: Path to marketplace.json
        repo_root: Absolute path to the repository root.  When provided,
            *marketplace_path* is relativized to avoid leaking the operator's
            filesystem layout into the prompt.
        marketplace_json: Compact marketplace payload to select from.

    Returns:
        Formatted advise prompt

    """
    safe_marketplace_path = _relativize_path(marketplace_path, repo_root)
    return PromptCatalog.current().render(
        "advise/advise.j2",
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        marketplace_path=safe_marketplace_path,
        marketplace_json=marketplace_json,
        terse_output_directive=get_terse_output_directive(),
    )


def get_codex_advise_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    marketplace_path: str,
    repo_root: str | None = None,
    marketplace_json: str = '{"plugins": []}',
) -> str:
    """Get the Codex advise prompt using the shared resolved marketplace path.

    Earlier Codex automation invoked the installed ``$advise`` skill from inside
    a nested ``codex exec`` run. That bypassed the shared Mnemosyne checkout
    lock/timeout in :mod:`advise_runner` and could leave the pipeline waiting on
    a second clone/update path. Codex now receives the same concrete
    ``marketplace.json`` path as Claude, plus constraints that keep the turn
    read-only and non-recursive.
    """
    safe_marketplace_path = _relativize_path(marketplace_path, repo_root)
    return PromptCatalog.current().render(
        "advise/direct.j2",
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        marketplace_path=safe_marketplace_path,
        marketplace_json=marketplace_json,
        terse_output_directive=get_terse_output_directive(),
    )


def get_advise_prompt_builder(agent: str) -> Callable[..., str]:
    """Return the provider-specific advise prompt builder."""
    if uses_direct_agent_runner(agent):
        return get_codex_advise_prompt
    return get_advise_prompt
