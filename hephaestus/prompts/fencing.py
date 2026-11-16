"""Nonce-delimited fencing primitives for untrusted prompt content.

Single source of truth for the injection defense shared by the automation
prompt layer and any library consumer that interpolates untrusted text
(e.g. fleet-sync conflict planning) into a prompt. The nonce makes it
infeasible for content to forge an end marker, even if a hostile payload
contains the literal string ``END_``.

This module also owns the untrusted-content notice render and the
prompt-scoped fencer: ``fence_content()`` mints one ``FencedContent``
with a fresh nonce, and the instance supplies the standard notice beside
its ``fence()`` calls (issue #3509).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from hephaestus.prompts.catalog import PromptCatalog

__all__ = ["FencedContent", "fence_content", "fence_untrusted", "get_untrusted_notice"]


def fence_untrusted(label: str, content: str, nonce: str) -> str:
    """Wrap untrusted content in nonce-delimited markers.

    Args:
        label: Self-describing block name used in logs and markers.
        content: Untrusted text that must not impersonate instructions.
        nonce: Random per-prompt token delimiting the block.

    Returns:
        The fenced block ``BEGIN_<nonce>_<label> ... END_<nonce>_<label>``.

    """
    return f"BEGIN_{nonce}_{label}\n{content}\nEND_{nonce}_{label}"


def get_untrusted_notice() -> str:
    """Render the shared untrusted-content notice from the active catalog."""
    return PromptCatalog.current().render("shared/untrusted_notice.j2")


@dataclass(frozen=True)
class FencedContent:
    """Prompt-scoped helper for fencing untrusted fields with one nonce."""

    nonce: str

    @property
    def untrusted_notice(self) -> str:
        """Return the standard untrusted-content notice for prompt templates."""
        return get_untrusted_notice()

    def fence(self, label: str, content: str) -> str:
        """Fence one untrusted prompt field using this prompt's nonce."""
        return fence_untrusted(label, content, self.nonce)


def fence_content() -> FencedContent:
    """Create a prompt-scoped fencer with a fresh random nonce."""
    return FencedContent(secrets.token_hex(8).upper())
