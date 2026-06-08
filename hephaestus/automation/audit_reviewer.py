"""Coordinator-pattern audit reviewer: one invocation reviews ALL open PRs.

Issue #994: ``PRReviewer`` (``hephaestus/automation/pr_reviewer.py``) spawns
one worker thread per PR; this module instead drives a single agent session
whose prompt enumerates every open PR, then parses one multi-PR JSON report.
Used for batch audits where per-PR sessions would saturate the agent
budget. Read-only: posts a summary-only review per PR, never commits or
pushes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import is_codex, run_codex_text
from hephaestus.cli.utils import add_json_arg, emit_json_status

from .claude_invoke import invoke_claude_with_session
from .claude_models import reviewer_model
from .claude_timeouts import pr_reviewer_claude_timeout
from .git_utils import get_repo_root, get_repo_slug
from .github_api import _gh_call, fetch_open_prs, gh_pr_review_post
from .session_naming import AGENT_PR_REVIEWER

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```json\s*\r?\n(.*?)\r?\n```", re.DOTALL)


def _parse_coordinator_results(text: str) -> list[dict[str, Any]]:
    """Extract every ```json fenced block; flatten ``audits`` lists.

    Coordinator may emit ONE block containing ``{"audits": [...]}`` or one
    block per PR (dict with ``pr_number``). Empty / whitespace-only / prose
    input → []. Malformed JSON inside a fence is skipped (WARN-logged) so
    one bad block does not lose the others. Extra fields preserved.
    """
    out: list[dict[str, Any]] = []
    for match in _FENCE_RE.finditer(text or ""):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed JSON block: %s", exc)
            continue
        if isinstance(payload, dict) and isinstance(payload.get("audits"), list):
            out.extend(p for p in payload["audits"] if isinstance(p, dict))
        elif isinstance(payload, dict) and "pr_number" in payload:
            out.append(payload)
    return out


def write_audit_report(state_dir: Path, audits: list[dict[str, Any]]) -> Path:
    """Persist audit results to ``<state_dir>/audit-report-<ts>.json``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = state_dir / f"audit-report-{ts}.json"
    path.write_text(json.dumps({"audits": audits, "generated_at": ts}, indent=2))
    return path


def print_audit_summary(audits: list[dict[str, Any]]) -> None:
    """Emit one INFO log line per audited PR (verdict + summary head)."""
    for a in audits:
        pr = a.get("pr_number", "?")
        verdict = a.get("verdict", "UNKNOWN")
        lines = (a.get("summary") or "").splitlines()
        summary = (lines[0] if lines else "")[:120]
        logger.info("PR #%s [%s] %s", pr, verdict, summary)


def _fetch_prs_by_number(numbers: list[int]) -> list[dict[str, Any]]:
    """Resolve explicit PR numbers via ``gh pr view``. Empty list → []."""
    if not numbers:
        return []
    out: list[dict[str, Any]] = []
    for n in numbers:
        try:
            r = _gh_call(["pr", "view", str(n), "--json", "number,title,headRefName,url,isDraft"])
            out.append(json.loads(r.stdout or "{}"))
        except Exception as exc:
            logger.warning("Failed to fetch PR #%s: %s", n, exc)
    return out


def _build_coordinator_prompt(prs: list[dict[str, Any]]) -> str:
    """Render the multi-PR audit prompt.

    The 'EXECUTE - do not return a plan' wording follows team knowledge;
    the 'do not background' wording follows known agent patterns.
    """
    lines = [
        "You are auditing multiple open PRs. EXECUTE the audit - do NOT return a plan.",
        "For EACH PR listed below, emit ONE ```json fenced block with keys:",
        "  pr_number (int), verdict (one of: GO|NOGO|UNSURE),",
        "  summary (str, <= 500 chars), findings (list[str]).",
        "Do NOT background. Do NOT exit early. Read-only tools only (Read/Grep/Glob).",
        "",
        "Open PRs:",
    ]
    for pr in prs:
        lines.append(f"- PR #{pr['number']}: {pr['title']} ({pr.get('url', '')})")
    return "\n".join(lines)


def run_audit_coordinator(
    *,
    prs: list[dict[str, Any]],
    agent: str,
    state_dir: Path,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Dispatch the coordinator agent; return parsed audit results.

    Raises RuntimeError on:
      - subprocess failure / timeout in the underlying agent runtime,
      - non-empty agent output that yields zero parseable JSON blocks
        (silent-empty guard: distinguishes 'agent ran but parser found
        nothing' from 'agent legitimately reported 0 audits').
    """
    if dry_run:
        logger.info("[DRY RUN] Would audit %d PR(s)", len(prs))
        return [
            {"pr_number": p["number"], "verdict": "UNSURE", "summary": "[DRY RUN]", "findings": []}
            for p in prs
        ]
    if not prs:
        return []
    prompt = _build_coordinator_prompt(prs)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / "audit-coordinator.log"
    try:
        if is_codex(agent):
            result = run_codex_text(
                prompt,
                cwd=get_repo_root(),
                timeout=pr_reviewer_claude_timeout(),
                sandbox="read-only",
            )
            response = result.stdout or ""
        else:
            # issue=0 is a safe sentinel for batch-audit context (no single issue).
            # session_name() converts to "0" and validates as non-empty; the UUIDv5
            # session persists across invocations of the same coordinator.
            stdout, _ = invoke_claude_with_session(
                repo=get_repo_slug(get_repo_root()),
                issue=0,
                agent=AGENT_PR_REVIEWER,
                prompt=prompt,
                model=reviewer_model(),
                cwd=get_repo_root(),
                timeout=pr_reviewer_claude_timeout(),
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Glob,Grep",
                input_via_stdin=True,
            )
            try:
                response = json.loads(stdout or "{}").get("result", stdout or "")
            except (json.JSONDecodeError, AttributeError):
                response = stdout or ""
        log_file.write_text(response)
    except subprocess.CalledProcessError as e:
        log_file.write_text(f"EXIT {e.returncode}\nSTDOUT:\n{e.stdout}\nSTDERR:\n{e.stderr}")
        raise RuntimeError(f"Audit coordinator failed: {e.stderr or e.stdout}") from e
    except subprocess.TimeoutExpired as e:
        log_file.write_text(f"TIMEOUT after {e.timeout}s\n{e.output or ''}")
        raise RuntimeError("Audit coordinator timed out") from e

    audits = _parse_coordinator_results(response)
    if response.strip() and not audits:
        raise RuntimeError("Coordinator returned no parseable JSON block")
    return audits


@dataclass
class AuditReviewer:
    """Run the coordinator audit and post a summary review per PR."""

    agent: str = "claude"
    pr_numbers: list[int] = field(default_factory=list)
    state_dir: Path | None = None
    dry_run: bool = False

    def __post_init__(self) -> None:
        """Set default state_dir if not provided."""
        if self.state_dir is None:
            self.state_dir = get_repo_root() / "build" / ".issue_implementer"

    def run(self) -> tuple[int, list[dict[str, Any]]]:
        """Run the coordinator audit and post a summary review per PR."""
        # __post_init__ guarantees state_dir is set; narrow type for mypy.
        if self.state_dir is None:  # pragma: no cover
            raise RuntimeError("state_dir unexpectedly None after __post_init__")
        prs = _fetch_prs_by_number(self.pr_numbers) if self.pr_numbers else fetch_open_prs()
        if not prs:
            logger.info("No PRs to audit")
            return 0, []
        try:
            audits = run_audit_coordinator(
                prs=prs,
                agent=self.agent,
                state_dir=self.state_dir,
                dry_run=self.dry_run,
            )
        except RuntimeError as exc:
            logger.error("Coordinator failed: %s", exc)
            return 1, []
        if not self.dry_run:
            report = write_audit_report(self.state_dir, audits)
            logger.info("Audit report written to %s", report)
        print_audit_summary(audits)
        for a in audits:
            try:
                gh_pr_review_post(
                    pr_number=int(a["pr_number"]),
                    comments=[],
                    summary=a.get("summary", ""),
                    event="COMMENT",
                    dry_run=self.dry_run,
                )
            except Exception as exc:
                logger.warning("Posting failed for PR #%s: %s", a.get("pr_number"), exc)
        return 0, audits


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hephaestus-audit-prs",
        description="Audit ALL open PRs in one coordinator agent invocation.",
    )
    parser.add_argument(
        "--pr-numbers",
        nargs="+",
        type=int,
        default=[],
        help="Audit only these PR numbers (default: all open).",
    )
    parser.add_argument("--codex", action="store_true", help="Use Codex instead of Claude.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Skip the agent call and the GitHub posting step."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging.")
    add_json_arg(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run the audit reviewer."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    reviewer = AuditReviewer(
        agent="codex" if args.codex else "claude",
        pr_numbers=args.pr_numbers,
        dry_run=args.dry_run,
    )
    rc, audits = reviewer.run()
    if getattr(args, "json", False):
        emit_json_status(rc, audits=len(audits))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
