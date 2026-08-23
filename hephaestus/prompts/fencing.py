"""Nonce-delimited fencing primitives for untrusted prompt content.

Single source of truth for the injection defense shared by the automation
prompt layer and any library consumer that interpolates untrusted text
(e.g. fleet-sync conflict planning) into a prompt. The nonce makes it
infeasible for content to forge an end marker, even if a hostile payload
contains the literal string ``END_``.
"""

from __future__ import annotations

__all__ = ["fence_untrusted"]


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
