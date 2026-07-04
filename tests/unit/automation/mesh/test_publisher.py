"""Tests for hephaestus.automation.mesh.publisher."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from hephaestus.automation.mesh.publisher import MeshPublisher, connect_kwargs


class FakeNC:
    """Minimal nats connection double."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.flushed = 0
        self.drained = False
        self.is_closed = False

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))

    async def flush(self, timeout: float | None = None) -> None:
        self.flushed += 1

    async def drain(self) -> None:
        self.drained = True
        self.is_closed = True


def _fake_connect(nc: FakeNC) -> Any:
    async def connect(url: str, **kwargs: Any) -> FakeNC:
        connect.calls.append((url, kwargs))  # type: ignore[attr-defined]
        return nc

    connect.calls = []  # type: ignore[attr-defined]
    return connect


class TestConnectKwargs:
    """Tests for ADR-008/009 connect options."""

    def test_empty_env(self) -> None:
        assert connect_kwargs({}) == {}

    def test_token(self) -> None:
        assert connect_kwargs({"NATS_CLIENT_TOKEN": "s3cret"}) == {"token": "s3cret"}

    def test_tls_context(self, tmp_path: Any) -> None:
        # An empty-but-present CA file exercises the context-creation path.
        import ssl

        ca = tmp_path / "ca.pem"
        ca.write_text("")
        try:
            kwargs = connect_kwargs({"NATS_CA_FILE": str(ca)})
        except ssl.SSLError:
            return  # platform rejects empty CA; the branch was still taken
        assert "tls" in kwargs


class TestMeshPublisher:
    """Tests for MeshPublisher."""

    def test_publish_encodes_json_and_flushes(self) -> None:
        nc = FakeNC()
        pub = MeshPublisher("nats://x:4222", connect=_fake_connect(nc))

        asyncio.run(pub.publish("hi.tasks.t.1.started", {"a": 1}))

        subject, data = nc.published[0]
        assert subject == "hi.tasks.t.1.started"
        assert json.loads(data.decode()) == {"a": 1}
        assert nc.flushed == 1

    def test_connect_is_idempotent(self) -> None:
        nc = FakeNC()
        connect = _fake_connect(nc)
        pub = MeshPublisher("nats://x:4222", connect=connect)

        async def run() -> None:
            await pub.connect()
            await pub.connect()

        asyncio.run(run())
        assert len(connect.calls) == 1

    def test_close_drains(self) -> None:
        nc = FakeNC()
        pub = MeshPublisher("nats://x:4222", connect=_fake_connect(nc))

        async def run() -> None:
            await pub.connect()
            await pub.close()

        asyncio.run(run())
        assert nc.drained is True
        assert pub.nc is None

    def test_close_without_connect_is_noop(self) -> None:
        pub = MeshPublisher("nats://x:4222", connect=_fake_connect(FakeNC()))
        asyncio.run(pub.close())
