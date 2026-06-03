"""Advise-phase prompt: search the team knowledge base before planning."""

from collections.abc import Callable

from hephaestus.agents.runtime import is_codex

from ._shared import _relativize_path

ADVISE_PROMPT = """
Search the team knowledge base for relevant prior learnings before planning this issue.

**Issue:** #{issue_number}: {issue_title}

{issue_body}

---

**Your task:**
1. Read the skills marketplace: {marketplace_path}
2. Search for plugins matching this issue's topic by:
   - Keywords in plugin names and descriptions
   - Tags and categories
   - Similar problem domains
3. For each relevant plugin, read its SKILL.md file to understand:
   - What worked (successful approaches)
   - What failed (common pitfalls)
   - Recommended parameters and configurations
   - Related patterns and conventions

**Output format:**
## Related Skills
| Plugin | Category | Relevance |
|--------|----------|-----------|
| plugin-name | category | Why it's relevant |

## What Worked
- Successful approach 1
- Successful approach 2

## What Failed
- Common pitfall 1 (from plugin X)
- Common pitfall 2 (from plugin Y)

## Recommended Parameters
- Parameter/configuration 1
- Parameter/configuration 2

If no relevant skills are found, output:
## Related Skills
None found

**Important:** Only return findings from the actual marketplace. Do not speculate or invent skills.
"""

CODEX_ADVISE_PROMPT = """$advise Search team knowledge before planning this issue.

Issue #{issue_number}: {issue_title}

Body:
{issue_body}

Return the advise findings only, in markdown. Do not implement or modify files.
"""


def get_advise_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    marketplace_path: str,
    repo_root: str | None = None,
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

    Returns:
        Formatted advise prompt

    """
    safe_marketplace_path = _relativize_path(marketplace_path, repo_root)
    return ADVISE_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        marketplace_path=safe_marketplace_path,
    )


def get_codex_advise_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    marketplace_path: str,
    repo_root: str | None = None,
) -> str:
    """Get the Codex advise prompt using Codex's ``$advise`` skill trigger.

    Codex skills are invoked with ``$skill-name`` rather than Claude slash
    commands. The marketplace path is accepted for the shared runner call
    signature but intentionally not interpolated: the installed Codex advise
    skill owns Mnemosyne clone/update and marketplace discovery.
    """
    del marketplace_path, repo_root
    return CODEX_ADVISE_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
    )


def get_advise_prompt_builder(agent: str) -> Callable[..., str]:
    """Return the provider-specific advise prompt builder."""
    if is_codex(agent):
        return get_codex_advise_prompt
    return get_advise_prompt
