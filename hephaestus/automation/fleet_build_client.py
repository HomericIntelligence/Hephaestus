"""Use one asynchronous Agamemnon client from the synchronous build supervisor."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agamemnon_client import AgamemnonClient


class BuildClientBridge:
    """Own one SDK client and event loop; leave retry decisions to the supervisor."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[AgamemnonClient]],
        supervisor_key: str,
        lifecycle_timeout: float = 5.0,
    ) -> None:
        """Retain operator configuration without creating a client or sending work."""
        if not supervisor_key:
            raise ValueError("a supervisor key is required")
        if not math.isfinite(lifecycle_timeout) or lifecycle_timeout <= 0:
            raise ValueError("lifecycle timeout must be finite and positive")
        self._factory = client_factory
        self._key = supervisor_key
        self._lifecycle_timeout = lifecycle_timeout
        self._owner = threading.get_ident()
        self._runner = asyncio.Runner()
        self._client: AgamemnonClient | None = None
        self._closed = False

    def _guard(self) -> None:
        if self._closed:
            raise RuntimeError("build client bridge is closed")
        if threading.get_ident() != self._owner:
            raise RuntimeError("build client bridge must use its owner thread")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError("build client bridge cannot run inside an async event loop")

    def _run[T](self, operation: Callable[[], Awaitable[T]], *, deadline: float) -> T:
        self._guard()
        if not math.isfinite(deadline):
            raise ValueError("build deadline must be finite")
        if deadline <= time.monotonic():
            raise TimeoutError("build deadline expired")

        async def bounded() -> T:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("build deadline expired")
            async with asyncio.timeout(remaining):
                return await operation()

        return self._runner.run(bounded())

    def __enter__(self) -> BuildClientBridge:
        """Create the configured SDK client on its one owned event loop."""
        self._guard()
        if self._client is not None:
            raise RuntimeError("build client bridge is already open")
        try:
            self._client = self._run(
                self._factory, deadline=time.monotonic() + self._lifecycle_timeout
            )
        except BaseException:
            self._closed = True
            self._runner.close()
            raise
        return self

    def _opened_client(self) -> AgamemnonClient:
        self._guard()
        if self._client is None:
            raise RuntimeError("build client bridge is not open")
        return self._client

    def claim_run(self, build_id: str, claim: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Send the retained claim once, within the caller's absolute deadline."""
        client = self._opened_client()
        return self._run(
            lambda: client.fleet_build_claim_run(build_id, claim, self._key), deadline=deadline
        )

    def publish_fact(
        self, build_id: str, fact: dict[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        """Send the retained fact once without creating a retry or task decision."""
        client = self._opened_client()
        return self._run(
            lambda: client.fleet_build_fact(build_id, fact, self._key), deadline=deadline
        )

    def close(self) -> None:
        """Close the SDK on its owner loop, then release that loop."""
        if self._closed:
            return
        self._guard()
        try:
            if self._client is not None:
                self._run(self._client.aclose, deadline=time.monotonic() + self._lifecycle_timeout)
        finally:
            self._closed = True
            self._client = None
            self._runner.close()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the owned client and loop when the synchronous context ends."""
        self.close()
