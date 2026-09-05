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
    StrictUndefined,
)

#: Packaged default templates, resolved by filesystem path relative to this
#: module so loading never depends on importlib package metadata (which races
#: an editable-install rebuild, #2308).
_DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "default"
_WRITING_STANDARD_TEMPLATE = "shared/writing_standard.j2"

# Complete agent-direction payloads. ``PromptCatalog.render`` applies the
# built-in writing policy after it resolves an optional harness overlay. This
# keeps the project policy in force even when the overlay replaces a complete
# prompt or the shared output fragment.
_AGENT_DIRECTION_TEMPLATES = frozenset(
    {
        "address_review/address_review.j2",
        "address_review/reply_recovery.j2",
        "advise/advise.j2",
        "advise/direct.j2",
        "advise/json_retry.j2",
        "agent_stage/skill_prefix.j2",
        "audit/coordinator.j2",
        "ci/fix.j2",
        "ci/force_engagement.j2",
        "fleet_sync/conflict_resolution.j2",
        "follow_up/follow_up.j2",
        "implementation/dirty_worktree.j2",
        "implementation/implementation.j2",
        "implementation/loop_review.j2",
        "implementation/resume_feedback.j2",
        "learn/learn.j2",
        "planning/context.j2",
        "planning/plan.j2",
        "planning/plan_loop_review.j2",
        "planning/plan_review.j2",
        "planning/requirements_recovery.j2",
        "planning/requirements_recovery_review.j2",
        "pr_management/commit_message.j2",
        "pr_management/pr_message.j2",
        "pr_review/analysis.j2",
        "pr_review/comment_difficulty.j2",
        "pr_review/validation.j2",
        "tidy/rebase_fix.j2",
    }
)
_COMPOSED_AGENT_DIRECTION_KEYS = {
    "advise/json_retry.j2": "advise_prompt",
    "planning/context.j2": "plan_prompt",
}

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
        # Resolve the packaged templates by filesystem path relative to this
        # module, NOT via ``PackageLoader``. PackageLoader consults importlib
        # package metadata, which is transiently inconsistent for the few
        # seconds after an editable-install rebuild — workers that import the
        # catalog in that window crash with "PackageLoader could not find
        # 'templates/default'" even though the files are on disk (#2308). A
        # ``__file__``-relative FileSystemLoader has no metadata dependency.
        loaders.append(FileSystemLoader(str(_DEFAULT_TEMPLATES_DIR)))
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
        self._writing_standard = self._writing_standard_directive()

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

    @staticmethod
    def _writing_standard_directive() -> str:
        """Read the immutable writing directive from the packaged default tree."""
        return (_DEFAULT_TEMPLATES_DIR / _WRITING_STANDARD_TEMPLATE).read_text(encoding="utf-8")

    @staticmethod
    def _compose_writing_standard(
        prompt: str, directive: str, *, preserve_leading_command: bool
    ) -> str:
        """Compose a previously loaded directive with one prompt."""
        if preserve_leading_command:
            return f"{prompt}\n\n{directive}"
        return f"{directive.rstrip()}\n\n{prompt}"

    def apply_writing_standard(self, prompt: str, *, preserve_leading_command: bool = False) -> str:
        """Apply the immutable writing directive to an arbitrary agent prompt."""
        directive = self._writing_standard
        return self._compose_writing_standard(
            prompt,
            directive,
            preserve_leading_command=preserve_leading_command,
        )

    def render(self, template_name: str, /, **context: Any) -> str:
        """Render one safe, relative prompt template name."""
        self._validate_template_name(template_name)
        if template_name not in _AGENT_DIRECTION_TEMPLATES:
            return self._environment.get_template(template_name).render(**context)

        directive = self._writing_standard
        context = dict(context)
        nested_key = _COMPOSED_AGENT_DIRECTION_KEYS.get(template_name)
        if nested_key is not None and isinstance(context.get(nested_key), str):
            prefix = f"{directive.rstrip()}\n\n"
            context[nested_key] = context[nested_key].removeprefix(prefix)
        if template_name == "address_review/address_review.j2":
            context["_writing_standard_directive"] = directive.rstrip()
        rendered = self._environment.get_template(template_name).render(**context)

        if template_name == "learn/learn.j2" or rendered.startswith("/"):
            # Provider command parsing requires the slash command to remain first.
            return self._compose_writing_standard(
                rendered, directive, preserve_leading_command=True
            )
        return self._compose_writing_standard(rendered, directive, preserve_leading_command=False)

    def source(self, template_name: str) -> str:
        """Return a template's source for legacy string-template compatibility."""
        self._validate_template_name(template_name)
        loader = self._environment.loader
        if loader is None:  # pragma: no cover - every catalog configures a loader
            raise RuntimeError("Prompt catalog has no template loader")
        source, _, _ = loader.get_source(self._environment, template_name)
        return source

    @staticmethod
    def _validate_template_name(template_name: str) -> None:
        """Reject absolute or traversal template names before loading."""
        path = PurePosixPath(template_name)
        if (
            path.is_absolute()
            or not template_name.endswith(".j2")
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in template_name
        ):
            raise ValueError(f"Invalid prompt template name: {template_name!r}")


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
    # ``PromptCatalog.current()`` is used by prompt builders that do not receive
    # an explicit catalog.  Reset that process-local selection before every
    # parse, including parses without ``--prompt-dir``: a previous in-process
    # command must not leak its harness overlay into the next command.
    if not getattr(parser, "_hephaestus_prompt_catalog_reset", False):
        parse_known_args = parser.parse_known_args

        def reset_catalog_before_parse(
            args: Sequence[str] | None = None,
            namespace: argparse.Namespace | None = None,
        ) -> tuple[argparse.Namespace, list[str]]:
            PromptCatalog.clear_current()
            return parse_known_args(args, namespace)

        parser.parse_known_args = reset_catalog_before_parse  # type: ignore[assignment]
        parser._hephaestus_prompt_catalog_reset = True  # type: ignore[attr-defined]

    parser.add_argument(
        "--prompt-dir",
        type=Path,
        action=_PromptDirAction,
        metavar="PATH",
        help="Optional directory layered over packaged Jinja prompt templates",
    )
