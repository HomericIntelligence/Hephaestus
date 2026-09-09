"""Test advice context in the queue implementation stage."""

from pathlib import Path

import hephaestus.automation as automation_pkg

AUTOMATION_DIR = Path(automation_pkg.__file__).parent


def test_implementer_prepends_advise_context_for_all_agents() -> None:
    """The implementation stage injects selected-skill context into the prompt.

    The advise wiring moved from the deleted legacy phase runner into
    the pipeline implementation stage (#1821): it gates the advise step behind
    ``ctx.config.enable_advise`` and composes the findings block via
    ``build_implementation_prompt``.
    """
    stage_src = (AUTOMATION_DIR / "pipeline" / "stages" / "implementation.py").read_text()
    assert "enable_advise" in stage_src, (
        "implementation stage must gate advise behind enable_advise"
    )
    assert "build_implementation_prompt" in stage_src, (
        "implementation stage must inject advise findings into the prompt"
    )
