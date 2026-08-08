"""Advise-phase prompt: select team knowledge to inject into automation prompts."""

from collections.abc import Callable

from hephaestus.agents.runtime import uses_direct_agent_runner

from ._shared import _relativize_path, fence_content, get_terse_output_directive
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
        issue_title: Untrusted GitHub issue title, fenced before interpolation.
        issue_body: Untrusted GitHub issue body, fenced before interpolation.
        marketplace_path: Path to marketplace.json
        repo_root: Absolute path to the repository root.  When provided,
            *marketplace_path* is relativized to avoid leaking the operator's
            filesystem layout into the prompt.
        marketplace_json: Untrusted marketplace payload, fenced before interpolation.

    Returns:
        Formatted advise prompt

    """
    safe_marketplace_path = _relativize_path(marketplace_path, repo_root)
    fenced = fence_content()
    return PromptCatalog.current().render(
        "advise/advise.j2",
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        marketplace_path=safe_marketplace_path,
        marketplace_json_block=fenced.fence("MARKETPLACE_JSON", marketplace_json),
        untrusted_notice=fenced.untrusted_notice,
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
    read-only and non-recursive. Untrusted issue and marketplace inputs are
    nonce-fenced before interpolation.
    """
    safe_marketplace_path = _relativize_path(marketplace_path, repo_root)
    fenced = fence_content()
    return PromptCatalog.current().render(
        "advise/direct.j2",
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        marketplace_path=safe_marketplace_path,
        marketplace_json_block=fenced.fence("MARKETPLACE_JSON", marketplace_json),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(),
    )


def get_advise_prompt_builder(agent: str) -> Callable[..., str]:
    """Return the provider-specific advise prompt builder."""
    if uses_direct_agent_runner(agent):
        return get_codex_advise_prompt
    return get_advise_prompt
