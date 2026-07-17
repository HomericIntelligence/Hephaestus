"""Jinja-backed loading for packaged and harness-specific agent prompts."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from jinja2 import (
    BaseLoader,
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    StrictUndefined,
)


class PromptCatalog:
    """Render registered prompt templates with an optional harness overlay.

    The override directory is intentionally a partial overlay: a template is
    loaded from it when present and otherwise falls through to the packaged
    default.  This lets a harness replace one prompt without copying the full
    default tree.
    """

    def __init__(self, override_root: Path | None = None) -> None:
        """Create a catalog with an optional directory layered over defaults."""
        loaders: list[BaseLoader] = []
        if override_root is not None:
            resolved_override = override_root.resolve()
            if not resolved_override.is_dir():
                raise ValueError(f"Prompt override directory does not exist: {override_root}")
            loaders.append(FileSystemLoader(str(resolved_override)))
        loaders.append(PackageLoader("hephaestus.automation.prompts", "templates/default"))
        self._environment = Environment(
            loader=ChoiceLoader(loaders),
            # Prompt templates are plain text; escaping would alter rendered
            # GitHub content and break the byte-parity compatibility contract.
            autoescape=False,  # nosec B701
            undefined=StrictUndefined,
            trim_blocks=False,
            lstrip_blocks=False,
            keep_trailing_newline=True,
            newline_sequence="\n",
        )

    @classmethod
    def from_environment(cls, *, override_root: Path | None = None) -> PromptCatalog:
        """Build a catalog using an explicit root or ``HEPHAESTUS_PROMPT_DIR``.

        The explicit value is the CLI integration point and intentionally wins
        over the environment so a recorded invocation is reproducible.
        """
        selected = override_root
        if selected is None:
            value = os.environ.get("HEPHAESTUS_PROMPT_DIR")
            selected = Path(value) if value else None
        return cls(override_root=selected)

    def render(self, template_name: str, /, **context: Any) -> str:
        """Render one safe, relative prompt template name."""
        path = PurePosixPath(template_name)
        if (
            path.is_absolute()
            or not template_name.endswith(".j2")
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in template_name
        ):
            raise ValueError(f"Invalid prompt template name: {template_name!r}")
        return self._environment.get_template(template_name).render(**context)
