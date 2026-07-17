"""Jinja-backed loading for packaged and harness-specific agent prompts."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextvars import ContextVar
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

_ACTIVE_CATALOG: ContextVar[PromptCatalog | None] = ContextVar(
    "hephaestus_active_prompt_catalog", default=None
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
        loaders.append(PackageLoader("hephaestus.prompts", "templates/default"))
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
    def from_cli(cls, *, override_root: Path | None = None) -> PromptCatalog:
        """Build a catalog from an explicit optional CLI override root."""
        return cls(override_root=override_root)

    @classmethod
    def current(cls) -> PromptCatalog:
        """Return the optional CLI-selected catalog or packaged defaults."""
        return _ACTIVE_CATALOG.get() or cls()

    @classmethod
    def clear_current(cls) -> None:
        """Clear CLI-selected state after an in-process invocation or test."""
        _ACTIVE_CATALOG.set(None)

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


class _PromptDirAction(argparse.Action):
    """Select the process-local prompt catalog from an explicit CLI value."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Path | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        if values is not None and not isinstance(values, (str, Path)):
            raise argparse.ArgumentError(self, "--prompt-dir requires one path")
        override_root = Path(values) if values is not None else None
        setattr(namespace, self.dest, override_root)
        _ACTIVE_CATALOG.set(PromptCatalog(override_root=override_root))


def add_prompt_dir_argument(parser: argparse.ArgumentParser) -> None:
    """Add the optional CLI-only harness prompt override selector."""
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        action=_PromptDirAction,
        metavar="PATH",
        help="Optional directory layered over packaged Jinja prompt templates",
    )
