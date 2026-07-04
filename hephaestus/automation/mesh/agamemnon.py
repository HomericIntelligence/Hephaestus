"""Minimal Agamemnon REST client for mesh workers.

Only the endpoints the worker loop needs (ADR-013): brief submission,
task state, and the overrun split. Auth is a bearer key from
``AGAMEMNON_API_KEY`` (Agamemnon also accepts ``X-API-Key``).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Callable
from typing import Any, cast

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class AgamemnonClient:
    """Tiny urllib-based client for the Agamemnon REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Store *base_url* / *api_key*; *urlopen* is injectable for tests."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._urlopen = urlopen
        self._timeout = timeout

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AgamemnonClient:
        """Build a client from ``AGAMEMNON_URL`` / ``AGAMEMNON_API_KEY``."""
        env = os.environ if environ is None else environ
        return cls(
            env.get("AGAMEMNON_URL", "http://localhost:8080"),
            env.get("AGAMEMNON_API_KEY"),
        )

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue one JSON request and return the decoded response body."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        with self._urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read()
        return cast(dict[str, Any], json.loads(raw.decode())) if raw else {}

    def submit_brief(self, brief: dict[str, Any]) -> dict[str, Any]:
        """POST a TaskBrief; returns the created L0–L3 plan."""
        return self._request("POST", "/v1/briefs", brief)

    def get_plan(self, brief_id: str) -> dict[str, Any]:
        """GET the full task tree for *brief_id*."""
        return self._request("GET", f"/v1/briefs/{brief_id}/plan")

    def task_state(self, task_id: str) -> dict[str, Any]:
        """GET current HMAS state for *task_id*."""
        return self._request("GET", f"/v1/tasks/{task_id}/state")

    def split_task(self, task_id: str, subtasks: list[dict[str, Any]]) -> dict[str, Any]:
        """Register overrun *subtasks* under *task_id* (ADR-013 §4).

        Each subtask dict carries ``title``, ``description`` and optionally
        ``blocked_by`` (task ids) and ``base_branch`` (the checkpoint branch).
        """
        return self._request("POST", f"/v1/tasks/{task_id}/split", {"subtasks": subtasks})

    def escalate(self, task_id: str, reason: str) -> dict[str, Any]:
        """POST an escalation for *task_id* (bottom-up delegation rule)."""
        return self._request("POST", f"/v1/tasks/{task_id}/escalate", {"reason": reason})
