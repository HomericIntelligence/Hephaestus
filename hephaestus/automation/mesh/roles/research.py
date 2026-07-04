"""Research chief-architect role: research an idea, interview the user, hand off.

The full intake segment of the pipeline (ADR-013 §5/§6, Odysseus
architecture.md steps 2–4): create the intake issue, generate clarifying
questions with Claude, run them through the interview relay (console live,
GitHub fallback, assumed on double timeout), research the idea into a brief,
describe the work as a Telemachy workflow, and register it as a GitHub epic
via ``telemachy register-epic`` (which publishes the epic trigger).

LLM work runs HERE, in a research-pool myrmidon — never inside Nestor.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from hephaestus.automation.mesh.worker import RoleResult, TaskContext
from hephaestus.io.utils import write_secure

logger = logging.getLogger(__name__)

INTAKE_MARKER = "<!-- hi:intake {intake_id} -->"
INTAKE_LABEL = "intake"
MESH_WORK_DIR = Path("build/.mesh")

QUESTIONS_PROMPT = """You are the research intake interviewer for this repository.
A user submitted this high-level task:

{untrusted_notice}

{idea_block}

{context_block}

List the clarifying questions whose answers would most change how this work
is scoped (target 2-3, fewer if the idea is already specific).
Return ONLY a JSON object: {{"questions": ["...", "..."]}}"""

BRIEF_PROMPT = """You are a research myrmidon producing a researched brief.

{untrusted_notice}

{idea_block}

{context_block}

{transcript_block}

Research this repository (read code/docs as needed) and write:
1. A researched brief: feasibility, prior art in this repo, proposed scope,
   and 1-2 ideated extensions worth filing as follow-ups.
2. A fenced ```yaml block containing a VALID telemachy/v1 workflow that
   describes 2-3 concrete implementation tasks, exactly this shape:

   apiVersion: telemachy/v1
   metadata:
     name: <kebab-case-name>
     description: <one line>
   agents:
     - name: task-agent
       program: claude-code
   teams:
     - name: implementation
       agents: [task-agent]
       tasks:
         - subject: <short imperative title>
           description: <what to implement and how to verify>
           assign_to: task-agent
           blocked_by: []   # list prior task subjects when ordering matters

   Task subjects become GitHub issue titles; blocked_by entries must
   reference other task subjects in the same team."""


def _default_invoke(prompt: str, ctx: TaskContext) -> str:
    """Run *prompt* through Claude with a per-intake resumable session."""
    from hephaestus.automation.claude_invoke import invoke_claude_with_session
    from hephaestus.automation.github_api import get_repo_info

    owner_repo = "/".join(get_repo_info())
    stdout, _stderr = invoke_claude_with_session(
        repo=owner_repo,
        issue=ctx.payload.get("issue") or ctx.task_id,
        # session_name() accepts only the known stage tokens; research runs
        # as a planning-class session.
        agent="planner",
        prompt=prompt,
        model=os.environ.get("MESH_MODEL", "sonnet"),
        cwd=Path.cwd(),
        timeout=int(os.environ.get("MESH_AGENT_TIMEOUT", "1800")),
        allowed_tools="Read,Glob,Grep",
        output_format="text",
    )
    return stdout


def _default_register_epic(workflow_path: Path, repo: str) -> dict[str, Any]:
    """Invoke ``telemachy register-epic`` and parse its JSON stdout."""
    cmd = [os.environ.get("TELEMACHY_BIN", "telemachy"), "register-epic", str(workflow_path)]
    if repo:
        cmd += ["--repo", repo]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=300)
    return cast(dict[str, Any], json.loads(result.stdout.strip().splitlines()[-1]))


def parse_questions(text: str) -> list[str]:
    """Extract the ``questions`` list from the model's JSON reply (max 3)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    questions = data.get("questions", [])
    return [str(q) for q in questions if str(q).strip()][:3]


def extract_workflow_yaml(text: str) -> str | None:
    """Extract the first fenced ```yaml block from the brief."""
    match = re.search(r"```ya?ml\n(.*?)```", text, re.DOTALL)
    return match.group(1) if match else None


def _workflow_path_for_intake(intake_id: str) -> Path:
    """Return a traversal-safe workflow path for an untrusted intake id."""
    filename_id = re.sub(r"[^A-Za-z0-9._-]+", "-", intake_id).strip(".-") or "unknown"
    workflow_path = MESH_WORK_DIR / f"intake-{filename_id}.workflow.yaml"
    mesh_dir = MESH_WORK_DIR.resolve()
    if not workflow_path.resolve().is_relative_to(mesh_dir):
        raise ValueError(f"workflow path escaped mesh work directory: {workflow_path}")
    return workflow_path


class ResearchHandler:
    """Runs intake → interview → research → epic registration."""

    def __init__(
        self,
        invoke: Callable[[str, TaskContext], str] | None = None,
        register_epic: Callable[[Path, str], dict[str, Any]] | None = None,
    ) -> None:
        """LLM invocation and epic registration are injectable for tests."""
        self._invoke = invoke or _default_invoke
        self._register_epic = register_epic or _default_register_epic

    def handle(self, ctx: TaskContext) -> RoleResult:
        """Work one research dispatch to a registered epic."""
        idea = str(ctx.payload.get("idea") or "").strip()
        if not idea:
            return RoleResult(
                ok=False,
                error_kind="BadDispatch",
                error_message="research payload missing 'idea'",
                retryable=False,
            )
        context = str(ctx.payload.get("context") or "")
        intake_id = str(ctx.payload.get("intake_id") or ctx.task_id)
        repo = str(ctx.payload.get("repo") or "")

        if ctx.payload.get("issue") is None:
            ctx.payload["issue"] = self._ensure_intake_issue(ctx, intake_id, idea, context)

        # Interview (each Q&A is mirrored to the intake issue for audit).
        # idea/context/transcript originate from external intake and GitHub
        # data — fence them like every other untrusted prompt field so
        # embedded instructions cannot escape into the agent prompt.
        from hephaestus.automation.prompts._shared import fence_content

        fenced = fence_content()
        idea_block = fenced.fence("IDEA", idea)
        context_block = fenced.fence("CONTEXT", context or "(none)")
        transcript_lines: list[str] = []
        for question in parse_questions(
            self._invoke(
                QUESTIONS_PROMPT.format(
                    untrusted_notice=fenced.untrusted_notice,
                    idea_block=idea_block,
                    context_block=context_block,
                ),
                ctx,
            )
        ):
            answer = ctx.ask(question)
            answered = (
                answer.answer if not answer.assumed else "(no answer — proceeding on assumptions)"
            )
            transcript_lines.append(f"Q: {question}\nA: {answered}")
            ctx.progress(f"**Q:** {question}\n**A:** {answered} _(via {answer.channel})_")
        transcript = "\n\n".join(transcript_lines) or "(no interview questions)"

        # Research → brief + workflow YAML.
        brief_text = self._invoke(
            BRIEF_PROMPT.format(
                untrusted_notice=fenced.untrusted_notice,
                idea_block=idea_block,
                context_block=context_block,
                transcript_block=fenced.fence("INTERVIEW_TRANSCRIPT", transcript),
            ),
            ctx,
        )
        ctx.progress(f"## Researched brief\n\n{brief_text}")
        workflow_yaml = extract_workflow_yaml(brief_text)
        if workflow_yaml is None:
            return RoleResult(
                ok=False,
                error_kind="NoWorkflow",
                error_message="brief contained no ```yaml workflow block",
                retryable=True,
            )

        # Describe via Telemachy → GitHub epic → hi.pipeline.epic.*.registered.
        workflow_path = _workflow_path_for_intake(intake_id)
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        write_secure(workflow_path, workflow_yaml)
        registered = self._register_epic(workflow_path, repo)
        epic_ref = registered.get("epic", "?")
        ctx.progress(f"Registered epic #{epic_ref} (key `{registered.get('key', '?')}`).")
        return RoleResult(
            ok=True,
            summary=f"intake {intake_id}: epic #{epic_ref} registered",
        )

    @staticmethod
    def _ensure_intake_issue(ctx: TaskContext, intake_id: str, idea: str, context: str) -> int:
        """Create (or on redelivery, find) the intake issue for *intake_id*."""
        from hephaestus.automation.github_api.issues import gh_issue_create
        from hephaestus.github.client import gh_call

        marker = INTAKE_MARKER.format(intake_id=intake_id)
        if ctx.is_redelivery:
            try:
                result = gh_call(
                    [
                        "issue",
                        "list",
                        "--label",
                        INTAKE_LABEL,
                        "--state",
                        "open",
                        "--search",
                        f'"{marker}" in:body',
                        "--json",
                        "number",
                        "--limit",
                        "5",
                    ]
                )
                found = json.loads(result.stdout)
                if found:
                    return int(found[0]["number"])
            except Exception as exc:
                logger.warning("intake issue search failed: %s", exc)
        body = (
            f"{marker}\n## Intake\n\n**Idea:** {idea}\n\n**Context:** {context or '(none)'}\n\n"
            "Interview transcript and researched brief follow as comments."
        )
        return gh_issue_create(f"[intake] {idea[:64]}", body, labels=[INTAKE_LABEL])
