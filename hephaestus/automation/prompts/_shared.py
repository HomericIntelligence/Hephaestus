"""Shared helpers and constants for prompt templates.

Provides the untrusted-input fencing helper used by every review prompt,
path-relativization, iteration helpers used by the loop prompts, and the
untrusted-content notice boilerplate.

Only the standard library is imported here — submodules in this package
build on these primitives.
"""

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

_prompts_logger = logging.getLogger("hephaestus.automation.prompts")


def _relativize_path(path: str, repo_root: str | None) -> str:
    """Return *path* relative to *repo_root* when possible.

    If *repo_root* is ``None`` or *path* is not under *repo_root*, the
    original *path* is returned unchanged and a warning is logged so
    operators know an absolute path is being injected.

    Args:
        path: Filesystem path to relativize.
        repo_root: Absolute repository root directory, or ``None``.

    Returns:
        A repo-relative path string (e.g. ``"worktrees/123-fix"``), or
        the original *path* if it cannot be made relative.

    """
    if not path:
        return path
    if repo_root is None:
        # Benign: an absolute path is a correct, working fallback. Logged at
        # DEBUG so it only surfaces under -v/--verbose rather than as routine
        # WARNING noise on every default run (#1556).
        _prompts_logger.debug(
            "repo_root not provided; injecting absolute path into prompt: %s", path
        )
        return path
    try:
        return str(Path(path).relative_to(repo_root))
    except ValueError:
        # Benign: cross-repo paths (e.g. the Mnemosyne marketplace) are
        # expected and the absolute path works. DEBUG, not WARNING (#1556).
        _prompts_logger.debug(
            "Path %r is not under repo_root %r; injecting absolute path into prompt.",
            path,
            repo_root,
        )
        return path


def get_untrusted_notice() -> str:
    """Render the shared untrusted-content notice from the active catalog."""
    from .catalog import PromptCatalog

    return PromptCatalog.current().render("shared/untrusted_notice.j2")


def _fence_untrusted(label: str, content: str, nonce: str) -> str:
    """Wrap untrusted content in nonce-delimited markers.

    The nonce makes it infeasible for content to forge an end marker, even if
    a malicious payload contains the literal string ``END_``. ``label`` makes
    each block self-describing in logs.
    """
    return f"BEGIN_{nonce}_{label}\n{content}\nEND_{nonce}_{label}"


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
        return _fence_untrusted(label, content, self.nonce)


def fence_content() -> FencedContent:
    """Create a prompt-scoped fencer with a fresh random nonce."""
    return FencedContent(secrets.token_hex(8).upper())


def _iteration_label(iteration: int) -> str:
    """Return a human-readable iteration label for review prompts."""
    return {0: "R0 (Initial review)", 1: "R1 (Re-review)", 2: "R2 (Final review)"}.get(
        iteration, f"R{iteration}"
    )


def _iteration_guidance(iteration: int) -> str:
    """Return guidance text emphasizing the iteration's role."""
    from .catalog import PromptCatalog

    return PromptCatalog.current().render("shared/iteration_guidance.j2", iteration=iteration)


def _prior_review_block(
    prior_review: str | None,
    fenced: FencedContent | None = None,
    *,
    label: str = "PRIOR_REVIEW",
) -> str:
    """Format the prior review (if any) as a context block."""
    if not prior_review:
        return ""
    body = fenced.fence(label, prior_review) if fenced is not None else prior_review
    from .catalog import PromptCatalog

    return PromptCatalog.current().render("shared/prior_review_block.j2", body=body)


# Token-reduction directive (#1082). Composed into every agent prompt via
# template-context injection. The GitHub-output
# carve-out MUST stay the first line so brevity never truncates pr-policy
# artifacts (see learn-agents-fabricate-closes-issue-numbers.md). The
# no-early-exit clause is bounded to *transient* external state so agents do
# NOT spin on permanent failures (auth, 4xx) — see
# swarm-agents-quit-early-on-polling.md.
def get_terse_output_directive() -> str:
    """Render the shared terse-output directive from the active catalog."""
    from .catalog import PromptCatalog

    return PromptCatalog.current().render("shared/terse_output_directive.j2")
