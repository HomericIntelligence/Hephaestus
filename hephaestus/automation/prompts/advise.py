"""Advise-phase prompt: select team knowledge to inject into automation prompts."""

from collections.abc import Callable

from hephaestus.agents.runtime import uses_direct_agent_runner

from ._shared import _TERSE_OUTPUT_DIRECTIVE, _relativize_path

ADVISE_PROMPT = """
Search ProjectMnemosyne for relevant prior learnings before this automation stage.

**Issue:** #{issue_number}: {issue_title}

{issue_body}

---

**Marketplace:** {marketplace_path}

```json
{marketplace_json}
```

---

{terse_output_directive}

**Your task:**
1. Search the marketplace entries above for skills matching this issue's topic by:
   - Keywords in plugin names and descriptions
   - Tags and categories
   - Similar problem domains
2. Select at most 5 skills whose files should be appended to the downstream prompt.
3. Do not modify files, implement code, post comments, or run write commands.

**Output format:**
Return only valid JSON with this exact shape:
{{
  "skills": [
    {{
      "name": "skill-name",
      "source": "./skills/skill-name.md",
      "reason": "One sentence explaining relevance to this issue."
    }}
  ]
}}

If no relevant skills are found, return:
{{"skills": []}}

**Important:** Only select skills from the actual marketplace. Do not speculate or invent skills.
"""

DIRECT_AGENT_ADVISE_PROMPT = (
    ADVISE_PROMPT
    + """

**Direct-agent automation constraints:**
- Do not invoke `$advise`; this prompt is already the advise step.
- Do not clone or update ProjectMnemosyne yourself; use the marketplace path above.
- Do not implement, commit, push, create a PR, or modify files.
"""
)
CODEX_ADVISE_PROMPT = DIRECT_AGENT_ADVISE_PROMPT


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
    return ADVISE_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        marketplace_path=safe_marketplace_path,
        marketplace_json=marketplace_json,
        terse_output_directive=_TERSE_OUTPUT_DIRECTIVE,
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
    return DIRECT_AGENT_ADVISE_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        marketplace_path=safe_marketplace_path,
        marketplace_json=marketplace_json,
        terse_output_directive=_TERSE_OUTPUT_DIRECTIVE,
    )


def get_advise_prompt_builder(agent: str) -> Callable[..., str]:
    """Return the provider-specific advise prompt builder."""
    if uses_direct_agent_runner(agent):
        return get_codex_advise_prompt
    return get_advise_prompt
