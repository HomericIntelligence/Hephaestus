"""Tests for hephaestus.automation.mesh.interview."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from hephaestus.automation.mesh.interview import Interviewer


class FakeSub:
    """Subscription double."""

    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeNC:
    """Connection double that lets tests deliver answer messages."""

    def __init__(self) -> None:
        self.callbacks: dict[str, Any] = {}
        self.sub = FakeSub()

    async def subscribe(self, subject: str, cb: Any) -> FakeSub:
        self.callbacks[subject] = cb
        return self.sub

    async def deliver(self, subject: str, payload: dict[str, Any]) -> None:
        class Msg:
            data = json.dumps(payload).encode()

        await self.callbacks[subject](Msg())


class FakePublisher:
    """Publisher double sharing the fake connection."""

    def __init__(self) -> None:
        self.nc = FakeNC()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> FakeNC:
        return self.nc

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, payload))


def test_console_answer_wins() -> None:
    """A live console answer resolves phase 1."""
    pub = FakePublisher()
    interviewer = Interviewer(pub, "in-1", console_timeout=1.0)

    async def run() -> Any:
        task = asyncio.ensure_future(interviewer.ask("Scope?", q_id="q1"))
        await asyncio.sleep(0.01)
        await pub.nc.deliver("hi.pipeline.interview.in-1.answer.q1", {"answer": "small slice"})
        return await task

    answer = asyncio.run(run())
    assert answer.answer == "small slice"
    assert answer.channel == "console"
    assert not answer.assumed
    # Question was published on the question subject.
    assert pub.published[0][0] == "hi.pipeline.interview.in-1.question.q1"
    assert pub.published[0][1]["question"] == "Scope?"
    assert pub.nc.sub.unsubscribed is True


def test_github_fallback_answer_republished() -> None:
    """Console timeout falls back to issue comments; answer mirrors onto NATS."""
    pub = FakePublisher()
    comments: list[list[dict[str, Any]]] = [
        [{"databaseId": 1, "body": "old comment"}],  # baseline
        [
            {"databaseId": 1, "body": "old comment"},
            {"databaseId": 2, "body": "<!-- hi:question in-1/q1 -->\n**Interview question:** X"},
            {"databaseId": 3, "body": "use podman"},
        ],
    ]
    posted: list[tuple[int, str]] = []

    interviewer = Interviewer(
        pub,
        "in-1",
        intake_issue=7,
        console_timeout=0.01,
        poll_interval=0.01,
        github_timeout=5.0,
        post_comment=lambda issue, body: posted.append((issue, body)),
        fetch_comments=lambda issue: comments.pop(0) if comments else [],
    )

    answer = asyncio.run(interviewer.ask("Container runtime?", q_id="q1"))

    assert answer.channel == "github"
    assert answer.answer == "use podman"
    assert posted[0][0] == 7
    assert "<!-- hi:question in-1/q1 -->" in posted[0][1]
    # Mirrored onto the answer subject with channel github.
    mirrored = [p for s, p in pub.published if s.endswith(".answer.q1")]
    assert mirrored and mirrored[0]["channel"] == "github"


def test_double_timeout_returns_assumed() -> None:
    """Both channels timing out yields an assumed answer + status republish."""
    pub = FakePublisher()
    interviewer = Interviewer(
        pub,
        "in-1",
        intake_issue=7,
        console_timeout=0.01,
        poll_interval=0.01,
        github_timeout=0.03,
        post_comment=lambda issue, body: None,
        fetch_comments=lambda issue: [],
    )

    answer = asyncio.run(interviewer.ask("Anything?", q_id="q2"))

    assert answer.assumed
    assert answer.channel == "assumed"
    statuses = [p.get("status") for _s, p in pub.published]
    assert "assumed" in statuses


def test_no_intake_issue_skips_github_phase() -> None:
    """Without an intake issue the ladder goes straight to assumed."""
    pub = FakePublisher()
    interviewer = Interviewer(pub, "in-2", console_timeout=0.01)

    answer = asyncio.run(interviewer.ask("Q?", q_id="q3"))

    assert answer.assumed


def test_malformed_console_answer_is_ignored() -> None:
    """A malformed answer payload does not resolve the future."""
    pub = FakePublisher()
    interviewer = Interviewer(pub, "in-3", console_timeout=0.05)

    async def run() -> Any:
        task = asyncio.ensure_future(interviewer.ask("Q?", q_id="q4"))
        await asyncio.sleep(0.01)

        class Msg:
            data = b"not json"

        await pub.nc.callbacks["hi.pipeline.interview.in-3.answer.q4"](Msg())
        return await task

    answer = asyncio.run(run())
    assert answer.assumed
