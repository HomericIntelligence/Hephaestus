"""Interview relay for research myrmidons (ADR-013 §5).

Questions go out on ``hi.pipeline.interview.{intake_id}.question.{q_id}``;
the console answers on the matching ``answer`` subject. Unanswered questions
fall back to GitHub comments on the intake issue, and a late console answer
always wins over a pending GitHub poll. Both channels timing out yields an
``assumed`` answer so research never deadlocks on an absent user.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hephaestus.automation.mesh.config import (
    envelope,
    interview_answer_subject,
    interview_question_subject,
)

logger = logging.getLogger(__name__)

QUESTION_MARKER = "<!-- hi:question {intake_id}/{q_id} -->"

#: Defaults per ADR-013 §5.
CONSOLE_TIMEOUT = 15 * 60
POLL_INTERVAL = 60
GITHUB_TIMEOUT = 24 * 60 * 60


@dataclass(frozen=True)
class InterviewAnswer:
    """Outcome of one interview question."""

    q_id: str
    answer: str
    channel: str  # "console" | "github" | "assumed"

    @property
    def assumed(self) -> bool:
        """True when both channels timed out and the worker must assume."""
        return self.channel == "assumed"


def _default_post_comment(issue_number: int, body: str) -> None:
    """Post *body* on *issue_number* via the automation gh helpers."""
    from hephaestus.automation.github_api.issues import gh_issue_comment

    gh_issue_comment(issue_number, body)


def _default_fetch_comments(issue_number: int) -> list[dict[str, Any]]:
    """Fetch recent comments as ``{databaseId, body}`` dicts."""
    from hephaestus.automation.github_api.issues import _fetch_issue_comment_ids

    return _fetch_issue_comment_ids(issue_number)


class Interviewer:
    """Asks the user questions over NATS with a GitHub-comment fallback."""

    def __init__(
        self,
        publisher: Any,
        intake_id: str,
        *,
        intake_issue: int | None = None,
        console_timeout: float = CONSOLE_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        github_timeout: float = GITHUB_TIMEOUT,
        post_comment: Callable[[int, str], None] = _default_post_comment,
        fetch_comments: Callable[[int], list[dict[str, Any]]] = _default_fetch_comments,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the relay; GitHub/comment callables are injectable for tests."""
        self._publisher = publisher
        self._intake_id = intake_id
        self._intake_issue = intake_issue
        self._console_timeout = console_timeout
        self._poll_interval = poll_interval
        self._github_timeout = github_timeout
        self._post_comment = post_comment
        self._fetch_comments = fetch_comments
        self._monotonic = monotonic

    async def ask(self, question: str, q_id: str | None = None) -> InterviewAnswer:
        """Ask *question* and return the answer per the ADR-013 fallback ladder."""
        q_id = q_id or uuid.uuid4().hex[:8]
        answer_subject = interview_answer_subject(self._intake_id, q_id)
        question_subject = interview_question_subject(self._intake_id, q_id)

        nc = await self._publisher.connect()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def _on_answer(msg: Any) -> None:
            if future.done():
                return
            try:
                import json

                payload = json.loads(msg.data.decode())
                answer = str(payload.get("answer", "")).strip()
            except Exception:
                return
            if answer:
                future.set_result(answer)

        sub = await nc.subscribe(answer_subject, cb=_on_answer)
        try:
            await self._publisher.publish(
                question_subject,
                envelope(intake_id=self._intake_id, q_id=q_id, question=question),
            )

            # Phase 1: live console answer.
            try:
                answer = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self._console_timeout
                )
                return InterviewAnswer(q_id=q_id, answer=answer, channel="console")
            except asyncio.TimeoutError:
                # Console timed out; continue into phase 2 so GitHub polling can still
                # capture a late console answer or a comment reply.
                logger.debug(
                    "Console answer timed out after %.0fs for %s; entering phase-2 GitHub fallback",
                    self._console_timeout,
                    q_id,
                )

            # Phase 2: GitHub fallback (console can still win mid-poll).
            if self._intake_issue is not None:
                result = await self._github_fallback(question, q_id, future)
                if result is not None:
                    if result.channel == "github":
                        # Mirror onto NATS so the transcript is complete.
                        await self._publisher.publish(
                            interview_answer_subject(self._intake_id, q_id),
                            envelope(
                                intake_id=self._intake_id,
                                q_id=q_id,
                                answer=result.answer,
                                channel="github",
                            ),
                        )
                    return result

            # Phase 3: proceed on stated assumptions.
            await self._publisher.publish(
                question_subject,
                envelope(
                    intake_id=self._intake_id,
                    q_id=q_id,
                    question=question,
                    status="assumed",
                ),
            )
            return InterviewAnswer(q_id=q_id, answer="", channel="assumed")
        finally:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()

    async def _github_fallback(
        self, question: str, q_id: str, future: asyncio.Future[str]
    ) -> InterviewAnswer | None:
        """Post the question on the intake issue and poll for a reply."""
        assert self._intake_issue is not None  # noqa: S101 - guarded by caller
        marker = QUESTION_MARKER.format(intake_id=self._intake_id, q_id=q_id)
        baseline = {c.get("databaseId") for c in self._fetch_comments(self._intake_issue)}
        self._post_comment(
            self._intake_issue,
            f"{marker}\n**Interview question:** {question}\n\n_Reply in a comment to answer._",
        )
        deadline = self._monotonic() + self._github_timeout
        while self._monotonic() < deadline:
            try:
                answer = await asyncio.wait_for(asyncio.shield(future), timeout=self._poll_interval)
                return InterviewAnswer(q_id=q_id, answer=answer, channel="console")
            except asyncio.TimeoutError:
                pass
            for comment in self._fetch_comments(self._intake_issue):
                body = str(comment.get("body", ""))
                if comment.get("databaseId") in baseline or "<!-- hi:question" in body:
                    continue
                answer = body.strip()
                if answer:
                    return InterviewAnswer(q_id=q_id, answer=answer, channel="github")
        return None
