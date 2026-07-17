"""Shared advise runner for every pipeline stage.

Each of the three session-stable stages (plan, implement, drive-green) begins
with an advise step that searches Mnemosyne for relevant prior
learnings before doing any work. The Mnemosyne clone/refresh, prompt
construction, and skip-marker conventions are identical across stages, so they
live here once (DRY) rather than being copied into each stage's advise step.

The only thing that differs per stage is *how* the selected agent is invoked.
Callers therefore pass an ``invoke`` callable that takes the skill-selection
prompt and returns the agent's JSON output; this module owns everything around
it.

Each stage gets the same result shape: a bounded ``## Selected Team Skills``
context block assembled from local Mnemosyne skill files and injected
explicitly into the downstream prompt.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hephaestus.constants import agent_clone_timeout, agent_git_timeout
from hephaestus.github.client import gh_call
from hephaestus.github.mnemosyne_repo import resolve_mnemosyne_target

# fcntl is POSIX-only; CPython does not bundle it on Windows. Import lazily so
# this module stays importable on Windows for tests that only need its
# pure-Python helpers. The cross-process file lock that uses fcntl is only
# reached on the live advise path.
try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - Windows fallback
    # WHY justified: mypy types the stdlib `fcntl` module, so rebinding the
    # name to None on the Windows fallback path needs [assignment].
    fcntl = None  # type: ignore[assignment]

from .claude_invoke import describe_claude_failure
from .git_utils import get_repo_root

logger = logging.getLogger(__name__)

# Single in-process lock guarding the clone/refresh of the shared Mnemosyne
# checkout. Module-level so every stage (and every parallel worker within a
# stage) serializes against the same lock — previously this lived on the
# Planner class, which left the implementer/CI-driver advise paths unguarded.
_MNEMOSYNE_LOCK = threading.Lock()
_MAX_SELECTED_SKILLS = 5
_MAX_SKILL_CONTEXT_CHARS = 40_000
_MAX_MARKETPLACE_PROMPT_CHARS = 80_000


@dataclass(frozen=True)
class SelectedSkill:
    """A skill selected by the model and validated against the local checkout."""

    name: str
    source: str
    reason: str
    path: Path


def advise_skipped(reason: str) -> str:
    """Return the marker string for a stage that ran without advise findings.

    A silent ``""`` made it impossible to tell whether advise wasn't attempted,
    was attempted but found nothing, or actually failed. The HTML comment is
    inert wherever the findings get interpolated (plan body, prompt context).
    """
    return f"<!-- advise step skipped: {reason} -->"


def default_mnemosyne_root() -> Path:
    """Return the shared Mnemosyne checkout root."""
    return Path.home() / ".agent-brain" / "Mnemosyne"


def _clone_mnemosyne(mnemosyne_root: Path) -> bool:
    """Clone the resolved Mnemosyne repository into ``mnemosyne_root``.

    The clone target is resolved via
    :func:`hephaestus.github.mnemosyne_repo.resolve_mnemosyne_target`, which
    prefers the gh-authenticated user's own fork (creating it if needed) and
    falls back to the upstream ``HomericIntelligence/Mnemosyne``.
    """
    timeout_s = agent_clone_timeout()
    target = resolve_mnemosyne_target()
    try:
        logger.info("Cloning %s to %s...", target.slug, mnemosyne_root)
        gh_call(
            [
                "repo",
                "clone",
                target.slug,
                str(mnemosyne_root),
            ],
            check=True,
            timeout=timeout_s,
        )
        logger.info("%s cloned successfully", target.slug)
        return True
    except subprocess.TimeoutExpired:
        logger.warning(
            "gh repo clone timed out after %s s; Mnemosyne unavailable this run",
            timeout_s,
        )
        return False
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to clone Mnemosyne: %s", e.stderr or e)
        return False
    except (RuntimeError, OSError) as e:
        logger.warning("Failed to clone Mnemosyne: %s", e)
        return False


def _is_valid_mnemosyne_checkout(mnemosyne_root: Path) -> bool:
    """Return True when an existing Mnemosyne path is a usable git checkout."""
    timeout_s = agent_git_timeout()
    try:
        result = subprocess.run(
            ["git", "-C", str(mnemosyne_root), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Failed to validate Mnemosyne checkout at %s: %s", mnemosyne_root, e)
        return False
    if result.returncode != 0 or result.stdout.strip() != "true":
        logger.warning(
            "Mnemosyne checkout at %s is invalid; stderr=%s",
            mnemosyne_root,
            (result.stderr or "").strip(),
        )
        return False
    return True


def ensure_mnemosyne(mnemosyne_root: Path) -> bool:
    """Clone Mnemosyne if absent, else fast-forward the existing clone.

    Uses an in-process lock plus a POSIX ``fcntl`` file lock so that multiple
    parallel workers (and multiple stages) never race on the shared checkout.

    Args:
        mnemosyne_root: Expected local path for Mnemosyne.

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
                            "Removing invalid Mnemosyne checkout at %s before re-clone",
                            mnemosyne_root,
                        )
                        shutil.rmtree(mnemosyne_root, ignore_errors=True)
                    else:
                        try:
                            timeout_s = agent_git_timeout()
                            subprocess.run(
                                ["git", "-C", str(mnemosyne_root), "pull", "--ff-only"],
                                check=True,
                                capture_output=True,
                                text=True,
                                timeout=timeout_s,
                            )
                            logger.debug("Mnemosyne refreshed at %s", mnemosyne_root)
                        except Exception as e:
                            logger.warning(
                                "Failed to refresh Mnemosyne (using existing clone): %s",
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
        return None, "Mnemosyne unavailable"

    marketplace_path = mnemosyne_root / ".claude-plugin" / "marketplace.json"
    if marketplace_path.exists():
        return marketplace_path, ""

    logger.warning(
        "Marketplace file not found at %s; attempting recovery re-clone of Mnemosyne",
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


def _repair_json_text(text: str) -> str:
    """Best-effort repair of near-JSON the selector model commonly emits.

    Handles two non-fatal shapes seen in real automation-loop runs (#1556):

    - Python-style single-quoted objects (``{'skills': []}``), which json
      rejects at char 1 with "Expecting property name enclosed in double
      quotes".
    - Trailing commas before ``]`` or ``}`` (``[{"a": 1},]``).

    Quotes are only flipped when the text contains no double quotes, so a
    valid JSON string that legitimately contains an apostrophe is never
    corrupted. The result is *attempted* JSON, not guaranteed valid — the
    caller still parses and validates it.
    """
    repaired = text
    if "'" in repaired and '"' not in repaired:
        repaired = repaired.replace("'", '"')
    # Drop trailing commas: `, ]` / `, }` (optionally with whitespace between).
    repaired = re.sub(r",(\s*[\]}])", r"\1", repaired)
    return repaired


def _extract_json_object(text: str) -> dict[str, object]:
    """Parse the selector's JSON object, allowing fenced or prefixed output.

    Recovers from the common malformed shapes the selector model emits:
    markdown code fences, prose prefixes, Python-style single quotes, and
    trailing commas (#1556). Strict JSON is always tried first.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty selector output")
    try:
        data: object = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ValueError("selector output did not contain a JSON object") from None
        candidate = stripped[start : end + 1]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                data = json.loads(_repair_json_text(candidate))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid selector JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("selector JSON must be an object")
    return data


def marketplace_prompt_payload(marketplace_path: Path) -> str:
    """Return a compact JSON payload of marketplace entries for model selection."""
    try:
        data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read Mnemosyne marketplace %s: %s", marketplace_path, exc)
        return '{"plugins": []}'

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return '{"plugins": []}'

    compact_plugins: list[dict[str, object]] = []
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        compact: dict[str, object] = {}
        for key in ("name", "description", "category", "tags", "source"):
            value = plugin.get(key)
            if isinstance(value, (str, list)):
                compact[key] = value
        if not compact:
            continue
        compact_plugins.append(compact)
        payload = json.dumps(
            {"plugins": compact_plugins},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if len(payload) > _MAX_MARKETPLACE_PROMPT_CHARS:
            compact_plugins.pop()
            break

    return json.dumps(
        {"plugins": compact_plugins},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _validate_skill_source(mnemosyne_root: Path, source: str) -> Path | None:
    """Return a safe local skill path for a marketplace source, or ``None``."""
    source_path = Path(source)
    if source_path.is_absolute() or ".." in source_path.parts:
        logger.warning("Ignoring unsafe Mnemosyne skill source: %s", source)
        return None

    try:
        skills_root = (mnemosyne_root / "skills").resolve(strict=False)
        candidate = (mnemosyne_root / source_path).resolve(strict=False)
        candidate.relative_to(skills_root)
    except ValueError:
        logger.warning("Ignoring non-skill Mnemosyne source: %s", source)
        return None

    if not candidate.is_file():
        logger.warning("Ignoring missing Mnemosyne skill source: %s", candidate)
        return None
    return candidate


def parse_selected_skills(selector_output: str, mnemosyne_root: Path) -> list[SelectedSkill]:
    """Parse and validate up to five selected skills from model JSON output."""
    data = _extract_json_object(selector_output)
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise ValueError("selector JSON must contain a skills list")

    selected: list[SelectedSkill] = []
    seen_paths: set[Path] = set()
    for item in skills:
        if len(selected) >= _MAX_SELECTED_SKILLS:
            break
        if not isinstance(item, dict):
            logger.warning("Ignoring malformed selected skill entry: %r", item)
            continue
        name = item.get("name")
        source = item.get("source")
        reason = item.get("reason")
        if not isinstance(name, str) or not isinstance(source, str) or not isinstance(reason, str):
            logger.warning("Ignoring selected skill entry with non-string fields: %r", item)
            continue

        path = _validate_skill_source(mnemosyne_root, source)
        if path is None or path in seen_paths:
            continue
        seen_paths.add(path)
        selected.append(
            SelectedSkill(
                name=name.strip() or path.stem,
                source=source.strip(),
                reason=reason.strip(),
                path=path,
            )
        )
    return selected


def format_selected_skill_context(
    selected: list[SelectedSkill],
    *,
    max_chars: int = _MAX_SKILL_CONTEXT_CHARS,
) -> str:
    """Read selected skill files and format a bounded prompt context block."""
    if not selected:
        return "## Selected Team Skills\n\nNone found."

    intro = "\n".join(
        [
            "## Selected Team Skills",
            "",
            "These Mnemosyne skills were selected for this issue. "
            + "Apply only the relevant guidance.",
        ]
    )
    parts = [intro]
    remaining = max_chars - len(intro)

    for skill in selected[:_MAX_SELECTED_SKILLS]:
        header = (
            f"\n### {skill.name}\n"
            f"Source: `{skill.source}`\n"
            f"Relevance: {skill.reason}\n\n"
            f"--- BEGIN SKILL {skill.name} ---\n"
        )
        footer = f"\n--- END SKILL {skill.name} ---"
        try:
            content = skill.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Failed to read selected Mnemosyne skill %s: %s", skill.path, exc)
            continue

        separator_len = 1
        overhead = separator_len + len(header) + len(footer)
        if remaining <= overhead:
            break
        budget = remaining - overhead
        truncated = len(content) > budget
        if truncated:
            marker = "\n[truncated]"
            content = content[: max(0, budget - len(marker))] + marker
        block = f"{header}{content}{footer}"
        parts.append(block)
        remaining -= separator_len + len(block)
        if truncated or remaining <= 0:
            break

    if len(parts) == 1:
        return "## Selected Team Skills\n\nNone found."
    return "\n".join(parts)


def run_advise(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    invoke: Callable[[str], str],
    build_prompt: Callable[..., str],
) -> str:
    """Run skill selection and return prompt-ready context (or a skip marker).

    Locates Mnemosyne (cloning/refreshing as needed), builds a compact
    marketplace-selection prompt, invokes the selected agent, validates the JSON
    response, then reads the selected skill files locally. Any failure degrades
    to an :func:`advise_skipped` marker so a stage never aborts just because
    advice could not be gathered.

    Args:
        issue_number: GitHub issue number (for prompt grounding + logging).
        issue_title: Issue title.
        issue_body: Issue body/description.
        invoke: Stage-specific callable that runs the selector prompt and
            returns the agent's JSON output.
        build_prompt: The advise prompt builder, injected so this module need
            not import the prompts package.

    Returns:
        Selected-skill context on success, or an ``advise_skipped`` marker.

    """
    try:
        repo_root = get_repo_root()
        mnemosyne_root = default_mnemosyne_root()

        marketplace_path, skip_reason = resolve_marketplace(mnemosyne_root)
        if marketplace_path is None:
            return advise_skipped(skip_reason)

        advise_prompt = build_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            marketplace_path=str(marketplace_path),
            repo_root=str(repo_root),
            marketplace_json=marketplace_prompt_payload(marketplace_path),
        )
        logger.info("Running advise skill selection for issue #%s...", issue_number)
        selector_output = invoke(advise_prompt)
        try:
            selected = parse_selected_skills(selector_output, mnemosyne_root)
        except ValueError as parse_err:
            # #1587: the selector model occasionally returns prose with no JSON
            # object. Retry ONCE with an explicit JSON-only reminder before
            # degrading to a skip — a malformed first response is usually
            # transient and a single re-ask recovers it.
            logger.warning(
                "Advise selector output for issue #%s was unparseable (%s); retrying once",
                issue_number,
                parse_err,
            )
            from hephaestus.prompts import PromptCatalog

            retry_prompt = PromptCatalog.current().render(
                "advise/json_retry.j2", advise_prompt=advise_prompt
            )
            selector_output = invoke(retry_prompt)
            selected = parse_selected_skills(selector_output, mnemosyne_root)
        return format_selected_skill_context(selected)
    except Exception as e:
        detail = describe_claude_failure(e)
        logger.warning(
            "Advise step failed for issue #%s: %s",
            issue_number,
            detail,
            extra={"exception_type": type(e).__name__},
            exc_info=True,
        )
        # The marker gets embedded in downstream prompts/plan bodies, so it
        # must carry the stderr detail too — a bare str(e) is just an exit
        # code for CalledProcessError (#1799).
        return advise_skipped(f"unexpected error: {detail}")
