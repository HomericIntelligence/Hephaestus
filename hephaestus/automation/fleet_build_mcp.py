"""Adapt prepared ordinary build contexts to optional project tools."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from mcp.server.lowlevel import Server

from .fleet_build_service import FleetBuildOwner

_server_log_context: ContextVar[bool] = ContextVar("fleet_build_mcp_logs", default=False)


class _ServerLogFilter(logging.Filter):
    """Keep SDK event levels without exposing protocol values in this server."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Remove protocol values only from records emitted in the server context."""
        if _server_log_context.get():
            record.msg = "Fleet build protocol event (payload omitted)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


@asynccontextmanager
async def _server_lifespan(_server: Server[Any]) -> AsyncIterator[dict[str, Any]]:
    """Scope root-direct and lowlevel SDK logs to this server's task lifetime."""
    loggers = (logging.getLogger(), logging.getLogger("mcp.server.lowlevel.server"))
    log_filter = _ServerLogFilter()
    token = _server_log_context.set(True)
    try:
        for logger in loggers:
            logger.addFilter(log_filter)
        yield {}
    finally:
        for logger in loggers:
            logger.removeFilter(log_filter)
        _server_log_context.reset(token)


class FleetBuildDispatcher:
    """Project ordinary builds from a fixed registry of trusted caller contexts."""

    _operations: ClassVar[dict[str, str]] = {
        "fleet_build_submit": "submit",
        "fleet_build_status": "status",
        "fleet_build_cancel": "cancel",
    }

    def __init__(
        self, contexts: Mapping[str, FleetBuildOwner | None], *, timeout: float = 30.0
    ) -> None:
        """Bind existing owners without opening a runtime, journal or backend."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("build tool timeout must be finite and at most 30 seconds")
        self._contexts = dict(contexts)
        self._timeout = timeout

    def tools(self) -> list[dict[str, Any]]:
        """Describe the three closed tools without importing an MCP SDK."""
        return [
            {
                "name": name,
                "description": (
                    description + " Uses a trusted context; does not verify build collection."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "contextId": {"type": "string", "minLength": 1, "maxLength": 128}
                    },
                    "required": ["contextId"],
                    "additionalProperties": False,
                },
            }
            for name, description in (
                ("fleet_build_submit", "Submit or explicitly replay the retained build intent."),
                ("fleet_build_status", "Read the controller's current build observation."),
                ("fleet_build_cancel", "Retain and send one cancellation, or replay it unchanged."),
            )
        ]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Use only an operator-prepared context for the selected tool."""
        if (
            name not in self._operations
            or type(arguments) is not dict
            or set(arguments) != {"contextId"}
            or not isinstance(arguments["contextId"], str)
            or arguments["contextId"] not in self._contexts
        ):
            raise ValueError("unknown build tool or context arguments")
        owner = self._contexts[arguments["contextId"]]
        if not isinstance(owner, FleetBuildOwner):
            raise RuntimeError("build context has no durable owner")
        operation = getattr(owner, self._operations[name])
        record = await operation(deadline=time.monotonic() + self._timeout)
        build = record["build"]
        terminal = build.get("terminal")
        if terminal is None:
            terminal = {}
        if type(terminal) is not dict:
            raise ValueError("invalid build terminal observation")
        outcome, cleanup = terminal.get("outcome"), terminal.get("cleanup")
        if outcome not in (
            None,
            "completed",
            "failed",
            "cancelled",
            "timed_out",
        ) or cleanup not in (
            None,
            "confirmed_empty",
        ):
            raise ValueError("invalid build terminal observation")
        return {
            "schema": "hi/hephaestus/build-observation/v1",
            "buildId": record["id"],
            "attempt": build["attempt"],
            "status": record["status"],
            "outcome": outcome,
            "cleanup": cleanup,
            "collectionVerified": False,
        }


def create_mcp_server(dispatcher: FleetBuildDispatcher) -> Server[Any]:
    """Expose three optional protocol tools over the existing ordinary owner."""
    from mcp import types
    from mcp.server.lowlevel import Server

    server: Server[Any] = Server("hephaestus-fleet-builds", version="1", lifespan=_server_lifespan)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**tool) for tool in dispatcher.tools()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            observation = await dispatcher.dispatch(name, arguments)
        except Exception:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Build operation unavailable.")],
                isError=True,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(observation, sort_keys=True))],
            structuredContent=observation,
            isError=False,
        )

    return server
