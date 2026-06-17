"""Shared advise runner for every pipeline stage.

Each of the three session-stable stages (plan, implement, drive-green) begins
with an advise step that searches ProjectMnemosyne for relevant prior
learnings before doing any work. The Mnemosyne clone/refresh, prompt
construction, and skip-marker conventions are identical across stages, so they
live here once (DRY) rather than being copied into ``planner.py``,
``implementer_phase_runner.py``, and ``ci_driver.py``.

The only thing that differs per stage is *how* the selected agent is invoked.
Callers therefore pass an ``invoke`` callable that takes the advise prompt and
returns the agent's text output; this module owns everything around it.

For the planner and CI-driver, the ``invoke`` callable targets ``AGENT_ADVISE``
(a distinct, cheap, read-only session) so the findings are returned as text and
injected into the stage's own prompt context.  The implementer's Claude path
instead targets ``AGENT_IMPLEMENTER`` with ``cwd=worktree_path`` so that the
advise step is the *first turn* of the implementer's session — the findings
live in the transcript and the implementation turn auto-resumes them via
``--resume`` without any text injection.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from hephaestus.github.client import gh_call

# fcntl is POSIX-only; CPython does not bundle it on Windows. Import lazily so
# this module stays importable on Windows for tests that only need its
# pure-Python helpers. The cross-process file lock that uses fcntl is only
# reached on the live advise path.
try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from .git_utils import get_repo_root

logger = logging.getLogger(__name__)

# Single in-process lock guarding the clone/refresh of the shared Mnemosyne
# checkout. Module-level so every stage (and every parallel worker within a
# stage) serializes against the same lock — previously this lived on the
# Planner class, which left the implementer/CI-driver advise paths unguarded.
_MNEMOSYNE_LOCK = threading.Lock()
_MNEMOSYNE_GIT_TIMEOUT = 30
_MNEMOSYNE_CLONE_TIMEOUT = 120


def advise_skipped(reason: str) -> str:
    """Return the marker string for a stage that ran without advise findings.

    A silent ``""`` made it impossible to tell whether advise wasn't attempted,
    was attempted but found nothing, or actually failed. The HTML comment is
    inert wherever the findings get interpolated (plan body, prompt context).
    """
    return f"<!-- advise step skipped: {reason} -->"


def _clone_mnemosyne(mnemosyne_root: Path) -> bool:
    """Clone the ProjectMnemosyne repository into ``mnemosyne_root``."""
    try:
        logger.info("Cloning ProjectMnemosyne to %s...", mnemosyne_root)
        gh_call(
            [
                "repo",
                "clone",
                "HomericIntelligence/ProjectMnemosyne",
                str(mnemosyne_root),
            ],
            check=True,
            timeout=_MNEMOSYNE_CLONE_TIMEOUT,
        )
        logger.info("ProjectMnemosyne cloned successfully")
        return True
    except subprocess.TimeoutExpired:
        logger.warning(
            "gh repo clone timed out after %s s; ProjectMnemosyne unavailable this run",
            _MNEMOSYNE_CLONE_TIMEOUT,
        )
        return False
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to clone ProjectMnemosyne: %s", e.stderr or e)
        return False
    except (RuntimeError, OSError) as e:
        logger.warning("Failed to clone ProjectMnemosyne: %s", e)
        return False


def _is_valid_mnemosyne_checkout(mnemosyne_root: Path) -> bool:
    """Return True when an existing ProjectMnemosyne path is a usable git checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(mnemosyne_root), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_MNEMOSYNE_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Failed to validate ProjectMnemosyne checkout at %s: %s", mnemosyne_root, e)
        return False
    if result.returncode != 0 or result.stdout.strip() != "true":
        logger.warning(
            "ProjectMnemosyne checkout at %s is invalid; stderr=%s",
            mnemosyne_root,
            (result.stderr or "").strip(),
        )
        return False
    return True


def ensure_mnemosyne(mnemosyne_root: Path) -> bool:
    """Clone ProjectMnemosyne if absent, else fast-forward the existing clone.

    Uses an in-process lock plus a POSIX ``fcntl`` file lock so that multiple
    parallel workers (and multiple stages) never race on the shared checkout.

    Args:
        mnemosyne_root: Expected local path for ProjectMnemosyne.

    Returns:
        True if the directory exists (or was cloned successfully), else False.

    """
    with _MNEMOSYNE_LOCK:
        lock_path = mnemosyne_root.parent / ".mnemosyne.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "w") as lock_file:
            # POSIX-only file locking; on Windows fcntl is None and we degrade
            # to the in-process thread lock acquired above.
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                # Re-check after acquiring the file lock. A previous process may
                # have completed or corrupted the checkout while we were waiting.
                if mnemosyne_root.exists():
                    if not _is_valid_mnemosyne_checkout(mnemosyne_root):
                        logger.warning(
                            "Removing invalid ProjectMnemosyne checkout at %s before re-clone",
                            mnemosyne_root,
                        )
                        shutil.rmtree(mnemosyne_root, ignore_errors=True)
                    else:
                        try:
                            subprocess.run(
                                ["git", "-C", str(mnemosyne_root), "pull", "--ff-only"],
                                check=True,
                                capture_output=True,
                                text=True,
                                timeout=_MNEMOSYNE_GIT_TIMEOUT,
                            )
                            logger.debug("ProjectMnemosyne refreshed at %s", mnemosyne_root)
                        except Exception as e:
                            logger.warning(
                                "Failed to refresh ProjectMnemosyne (using existing clone): %s",
                                e,
                            )
                        return True

                cloned = _clone_mnemosyne(mnemosyne_root)
                # Do NOT unlink lock_path here — the file-lock sentinel must
                # remain on disk until the fd closes in the finally block.
                # Unlinking while LOCK_EX is held lets a second process open a
                # new inode at the same path and grab its own lock, breaking
                # cross-process mutual exclusion (#370).
                return cloned
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def resolve_marketplace(mnemosyne_root: Path) -> tuple[Path | None, str]:
    """Ensure Mnemosyne is present and return its ``marketplace.json`` path.

    Recovers once from a missing marketplace file by re-cloning.

    Returns:
        ``(marketplace_path, "")`` on success, or ``(None, reason)`` when
        Mnemosyne (or its manifest) cannot be made available. The ``reason``
        is forwarded into the :func:`advise_skipped` breadcrumb so a reader can
        tell "clone failed" apart from "manifest missing".

    """
    if not mnemosyne_root.exists() and not ensure_mnemosyne(mnemosyne_root):
        return None, "ProjectMnemosyne unavailable"

    marketplace_path = mnemosyne_root / ".claude-plugin" / "marketplace.json"
    if marketplace_path.exists():
        return marketplace_path, ""

    logger.warning(
        "Marketplace file not found at %s; attempting recovery re-clone of ProjectMnemosyne",
        marketplace_path,
    )
    shutil.rmtree(mnemosyne_root, ignore_errors=True)
    if not ensure_mnemosyne(mnemosyne_root) or not marketplace_path.exists():
        logger.error(
            "Recovery failed: marketplace.json still missing at %s; skipping advise step",
            marketplace_path,
        )
        return None, f"marketplace.json missing at {marketplace_path}"
    return marketplace_path, ""


def run_advise(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    invoke: Callable[[str], str],
    build_prompt: Callable[..., str],
) -> str:
    """Run the advise step and return findings (or a skip marker).

    Locates ProjectMnemosyne (cloning/refreshing as needed), builds the advise
    prompt, and invokes the selected agent via the stage-supplied ``invoke`` callable. Any
    failure degrades to an :func:`advise_skipped` marker so a stage never aborts
    just because advice could not be gathered.

    Args:
        issue_number: GitHub issue number (for prompt grounding + logging).
        issue_title: Issue title.
        issue_body: Issue body/description.
        invoke: Stage-specific callable that runs the advise prompt under
            ``AGENT_ADVISE`` and returns the agent's text output.
        build_prompt: The advise prompt builder (``prompts.get_advise_prompt``),
            injected so this module need not import the prompts package.

    Returns:
        Findings text on success, or an ``advise_skipped`` marker string.

    """
    try:
        repo_root = get_repo_root()
        mnemosyne_root = repo_root / "build" / "ProjectMnemosyne"

        marketplace_path, skip_reason = resolve_marketplace(mnemosyne_root)
        if marketplace_path is None:
            return advise_skipped(skip_reason)

        advise_prompt = build_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            marketplace_path=str(marketplace_path),
            repo_root=str(repo_root),
        )
        logger.info("Running advise for issue #%s...", issue_number)
        return invoke(advise_prompt)
    except Exception as e:
        logger.warning("Advise step failed for issue #%s: %s", issue_number, e)
        return advise_skipped(f"unexpected error: {e}")
