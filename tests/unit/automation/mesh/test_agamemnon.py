"""Tests for hephaestus.automation.mesh.agamemnon."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from hephaestus.automation.mesh.agamemnon import AgamemnonClient


class FakeOpener:
    """Records requests and returns canned JSON responses."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.requests: list[Any] = []
        self.response = response or {}

    @contextmanager
    def __call__(self, req: Any, timeout: int = 0) -> Iterator[io.BytesIO]:
        self.requests.append(req)
        yield io.BytesIO(json.dumps(self.response).encode())


class TestAgamemnonClient:
    """Tests for request composition."""

    def test_split_task_posts_subtasks(self) -> None:
        opener = FakeOpener({"created": 2})
        client = AgamemnonClient("http://a:8080/", api_key="k", urlopen=opener)

        result = client.split_task(
            "t-1",
            [{"title": "rest", "description": "d", "blocked_by": [], "base_branch": "b"}],
        )

        assert result == {"created": 2}
        req = opener.requests[0]
        assert req.full_url == "http://a:8080/v1/tasks/t-1/split"
        assert req.get_method() == "POST"
        assert req.get_header("Authorization") == "Bearer k"
        body = json.loads(req.data.decode())
        assert body["subtasks"][0]["title"] == "rest"

    def test_submit_brief(self) -> None:
        opener = FakeOpener({"brief": {"id": "b-1"}})
        client = AgamemnonClient("http://a:8080", urlopen=opener)

        result = client.submit_brief({"title": "T"})

        assert result["brief"]["id"] == "b-1"
        assert opener.requests[0].full_url == "http://a:8080/v1/briefs"
        # No API key → no Authorization header.
        assert opener.requests[0].get_header("Authorization") is None

    def test_task_state_get(self) -> None:
        opener = FakeOpener({"state": "InProgress"})
        client = AgamemnonClient("http://a:8080", urlopen=opener)

        assert client.task_state("t-2")["state"] == "InProgress"
        req = opener.requests[0]
        assert req.full_url == "http://a:8080/v1/tasks/t-2/state"
        assert req.get_method() == "GET"
        assert req.data is None

    def test_escalate(self) -> None:
        opener = FakeOpener({"escalated": True})
        client = AgamemnonClient("http://a:8080", urlopen=opener)

        client.escalate("t-3", "stuck")
        body = json.loads(opener.requests[0].data.decode())
        assert body == {"reason": "stuck"}

    def test_from_env(self) -> None:
        client = AgamemnonClient.from_env(
            {"AGAMEMNON_URL": "http://x:1", "AGAMEMNON_API_KEY": "kk"}
        )
        assert client._base_url == "http://x:1"
        assert client._api_key == "kk"
