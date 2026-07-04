"""Mesh worker claim loop (ADR-013 §1/§2/§4).

One :class:`MeshWorker` serves one (domain, role) queue: it pulls one task at
a time from the durable consumer, publishes the ``started`` fact (claim =
assignment), heartbeats the lease every 5 minutes while the role handler
runs, and publishes ``completed``/``failed`` before acking. Redeliveries are
surfaced to the handler via ``ctx.attempt`` so it can run its idempotency
preamble (check branch/PR/labels, resume or no-op). The ~1 h overrun
re-adjustment is cooperative: handlers check :meth:`TaskContext.overrun`
between phases and call :meth:`TaskContext.split` to checkpoint and register
the remainder as sub-tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

from hephaestus.automation.mesh.agamemnon import AgamemnonClient
from hephaestus.automation.mesh.config import (
    DISPATCH_STREAM,
    MeshConfig,
    envelope,
    parse_dispatch_subject,
    task_event_subject,
)
from hephaestus.automation.mesh.publisher import MeshPublisher

logger = logging.getLogger(__name__)

CHECKPOINT_MARKER = "<!-- hi:checkpoint {task_id} -->"

#: fetch() poll timeout — short enough for prompt shutdown, long enough to
#: avoid hammering the server.
FETCH_TIMEOUT = 30


@dataclass
class RoleResult:
    """Outcome of one role-handler invocation."""

    ok: bool
    summary: str = ""
    pr: dict[str, Any] | None = None
    error_kind: str = ""
    error_message: str = ""
    retryable: bool = True


@dataclass
class TaskContext:
    """Everything a role handler may touch while working one task."""

    config: MeshConfig
    payload: dict[str, Any]
    task_id: str
    team_id: str
    attempt: int
    publisher: MeshPublisher
    agamemnon: AgamemnonClient
    deadline: float
    loop: asyncio.AbstractEventLoop | None = None
    _monotonic: Any = field(default=time.monotonic, repr=False)

    @property
    def is_redelivery(self) -> bool:
        """Return True when JetStream has delivered this task before (attempt > 1)."""
        return self.attempt > 1

    def ask(self, question: str, q_id: str | None = None) -> Any:
        """Ask the user *question* over the interview relay (ADR-013 §5).

        Handlers run in a worker thread while the relay is async on the
        worker loop, so this bridges with ``run_coroutine_threadsafe`` and
        blocks until an answer (console, GitHub fallback, or assumed).
        """
        from hephaestus.automation.mesh.interview import Interviewer

        if self.loop is None:
            raise RuntimeError("TaskContext.ask requires a running worker loop")
        interviewer = Interviewer(
            self.publisher,
            intake_id=str(self.payload.get("intake_id") or self.task_id),
            intake_issue=self.payload.get("issue"),
        )
        future = asyncio.run_coroutine_threadsafe(interviewer.ask(question, q_id), self.loop)
        return future.result()

    def overrun(self) -> bool:
        """Return True once ~1 h of active work has elapsed (ADR-013 §4)."""
        return bool(self._monotonic() >= self.deadline)

    def progress(self, text: str) -> None:
        """Post a progress comment on the task's GitHub issue (best-effort).

        Progress comments are the resume anchor for redeliveries; handlers
        should call this at every major step. Falls back to logging when the
        payload carries no issue or GitHub is unreachable.
        """
        issue = self.payload.get("issue")
        if issue is not None:
            try:
                from hephaestus.automation.github_api.issues import gh_issue_comment

                gh_issue_comment(int(issue), text)
                return
            except Exception as exc:
                logger.warning("progress comment failed for #%s: %s", issue, exc)
        logger.info("[progress %s] %s", self.task_id, text)

    def split(self, subtasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Checkpoint and register overrun *subtasks* via Agamemnon (ADR-013 §4).

        The handler is expected to have committed/pushed its branch already;
        this posts the checkpoint marker comment and registers the remainder.
        The current task then completes as the first slice of the split.
        """
        response = self.agamemnon.split_task(self.task_id, subtasks)
        marker = CHECKPOINT_MARKER.format(task_id=self.task_id)
        titles = ", ".join(str(s.get("title", "?")) for s in subtasks)
        self.progress(f"{marker}\nOverrun checkpoint: remainder split into {titles}.")
        return response


class RoleHandler(Protocol):
    """A role handler works one claimed task to completion."""

    def handle(self, ctx: TaskContext) -> RoleResult:
        """Run the role's work for *ctx* (called in a worker thread)."""
        raise NotImplementedError  # pragma: no cover


class MeshWorker:
    """Claim loop for one (domain, role) queue."""

    def __init__(
        self,
        config: MeshConfig,
        handler: RoleHandler,
        *,
        publisher: MeshPublisher | None = None,
        agamemnon: AgamemnonClient | None = None,
    ) -> None:
        """Wire the worker; publisher/agamemnon are injectable for tests."""
        self.config = config
        self.handler = handler
        self.publisher = publisher or MeshPublisher(config.nats_url)
        self.agamemnon = agamemnon or AgamemnonClient(
            config.agamemnon_url,
            os.environ.get("AGAMEMNON_API_KEY"),
        )

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Consume the queue until *stop* is set (or SIGTERM/SIGINT).

        Signal handlers set the stop event so the in-flight handler finishes
        and the loop exits at the next fetch boundary — a drain, not a kill
        (an abandoned claim would otherwise redeliver only after the full
        AckWait). Callers may still pass their own *stop* event.
        """
        from nats.js.api import AckPolicy, ConsumerConfig

        if stop is None:
            stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, sig_name, None)
            if signum is not None:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.add_signal_handler(signum, stop.set)

        nc = await self.publisher.connect()
        js = nc.jetstream()
        psub = await js.pull_subscribe(
            self.config.filter_subject,
            durable=self.config.durable_name,
            stream=DISPATCH_STREAM,
            config=ConsumerConfig(
                durable_name=self.config.durable_name,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=self.config.ack_wait_seconds,
                max_deliver=self.config.max_deliver,
                max_ack_pending=self.config.max_ack_pending,
                filter_subject=self.config.filter_subject,
            ),
        )
        logger.info(
            "worker %s consuming %s (durable %s)",
            self.config.agent_id,
            self.config.filter_subject,
            self.config.durable_name,
        )
        while not stop.is_set():
            try:
                msgs = await psub.fetch(1, timeout=FETCH_TIMEOUT)
            except TimeoutError:
                continue
            except asyncio.TimeoutError:
                continue
            for msg in msgs:
                try:
                    await self.handle_message(msg)
                except Exception:
                    # handle_message resolves every normal path itself; only a
                    # failed ack/nak/term (or a bug) reaches here. The claim
                    # redelivers after AckWait — losing one message's ack must
                    # never take the whole long-running consumer down.
                    logger.exception("handle_message crashed; continuing consume loop")

    async def handle_message(self, msg: Any) -> None:
        """Work one claimed dispatch message end to end."""
        try:
            payload = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.error("malformed dispatch payload on %s; terminating", msg.subject)
            await msg.term()
            return
        if not isinstance(payload, dict):
            # Valid-but-non-object JSON (`[]`, `"x"`, `3`) would raise on the
            # first .get() below — outside any handler try — leaving the poison
            # message neither acked, naked, nor termed.
            logger.error(
                "non-object dispatch payload (%s) on %s; terminating",
                type(payload).__name__,
                msg.subject,
            )
            await msg.term()
            return

        parsed = parse_dispatch_subject(msg.subject)
        task_id = str(payload.get("task_id") or (parsed[2] if parsed else "")) or "unknown"
        team_id = str(payload.get("team_id") or self.config.team_id)
        attempt = self._delivery_attempt(msg)

        ctx = TaskContext(
            config=self.config,
            payload=payload,
            task_id=task_id,
            team_id=team_id,
            attempt=attempt,
            publisher=self.publisher,
            agamemnon=self.agamemnon,
            deadline=time.monotonic() + self.config.overrun_seconds,
            loop=asyncio.get_running_loop(),
        )

        # State-fact publishes are best-effort observability: a NATS hiccup
        # here must never crash the consume loop or decide the message's fate
        # — only ack/nak/term may do that (the ADR-013 assignment record is
        # redundant with JetStream's own delivery state).
        await self._publish_event_safe(ctx, "started", {})
        heartbeat = asyncio.create_task(self._heartbeat_loop(msg, ctx))
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mesh-role") as executor:
                result = await asyncio.get_running_loop().run_in_executor(
                    executor, self.handler.handle, ctx
                )
        except Exception as exc:
            logger.exception("handler crashed for task %s", task_id)
            result = RoleResult(
                ok=False,
                error_kind=type(exc).__name__,
                error_message=str(exc),
                retryable=True,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        if result.ok:
            extra: dict[str, Any] = {"summary": result.summary}
            if result.pr:
                extra["pr"] = result.pr
            await self._publish_event_safe(ctx, "completed", extra)
            await msg.ack()
        else:
            await self._publish_event_safe(
                ctx,
                "failed",
                {
                    "error": {
                        "kind": result.error_kind,
                        "message": result.error_message,
                        "retryable": result.retryable,
                    }
                },
            )
            if result.retryable:
                await msg.nak()
            else:
                await msg.term()

    async def _publish_event_safe(self, ctx: TaskContext, verb: str, extra: dict[str, Any]) -> None:
        """Publish a state fact without letting a NATS failure decide the message's fate.

        A publish failure before the handler ran would otherwise crash the
        consume loop with the message unacked; one after a successful handler
        would skip the ack and re-run completed work. The fact is logged and
        dropped — JetStream's own delivery state remains authoritative.
        """
        try:
            await self._publish_event(ctx, verb, extra)
        except Exception as exc:
            logger.warning(
                "state-fact publish failed for task %s verb=%s: %s", ctx.task_id, verb, exc
            )

    async def _publish_event(self, ctx: TaskContext, verb: str, extra: dict[str, Any]) -> None:
        """Publish one ADR-013 §2 state fact (claim/assignment lives here)."""
        await self.publisher.publish(
            task_event_subject(ctx.team_id, ctx.task_id, verb),
            envelope(
                task_id=ctx.task_id,
                team_id=ctx.team_id,
                domain=self.config.domain,
                role=self.config.role,
                agent_id=self.config.agent_id,
                exec_host=self.config.exec_host,
                attempt=ctx.attempt,
                **extra,
            ),
        )

    async def _heartbeat_loop(self, msg: Any, ctx: TaskContext) -> None:
        """Extend the lease every heartbeat interval while the handler runs.

        Also observes the ADR-013 overrun checkpoint: role handlers run as
        monolithic operations, so the worker — not the handler — is the one
        place the ~1 h soft deadline is reliably seen. On the first heartbeat
        past the deadline it publishes an ``updated`` state fact flagged
        ``overrun`` so Agamemnon/operators can checkpoint or split
        (``POST /v1/tasks/:id/split``); the lease itself keeps extending —
        overrun is a soft signal, never a kill (ADR-013 §4).
        """
        overrun_reported = False
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            try:
                await msg.in_progress()
            except Exception as exc:
                logger.warning("heartbeat failed: %s", exc)
            if not overrun_reported and ctx.overrun():
                overrun_reported = True
                logger.warning(
                    "task %s exceeded the %ss overrun threshold; publishing checkpoint",
                    ctx.task_id,
                    self.config.overrun_seconds,
                )
                with contextlib.suppress(Exception):
                    await self._publish_event(
                        ctx,
                        "updated",
                        {
                            "overrun": True,
                            "overrun_seconds": self.config.overrun_seconds,
                            "note": (
                                "soft deadline exceeded; operator/Agamemnon may "
                                "checkpoint or split via POST /v1/tasks/:id/split"
                            ),
                        },
                    )

    @staticmethod
    def _delivery_attempt(msg: Any) -> int:
        """Read JetStream's delivery counter (1 on first delivery)."""
        try:
            return int(msg.metadata.num_delivered)
        except (AttributeError, TypeError, ValueError):
            return 1
