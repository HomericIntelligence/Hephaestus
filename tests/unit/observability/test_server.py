"""Unit tests for hephaestus.observability.server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from hephaestus.observability.metrics import MetricsRegistry
from hephaestus.observability.server import MetricsHTTPServer


@pytest.fixture
def registry() -> MetricsRegistry:
    """Return a fresh, empty metrics registry."""
    return MetricsRegistry()


@pytest.fixture
def server(registry: MetricsRegistry) -> Iterator[MetricsHTTPServer]:
    """Start a MetricsHTTPServer on an ephemeral port and stop it on teardown."""
    srv = MetricsHTTPServer(registry, port=0)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _get(server: MetricsHTTPServer, path: str) -> tuple[int, dict[str, str], bytes]:
    """Fetch *path* from *server* and return (status, headers, body)."""
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class TestLifecycle:
    """Tests for MetricsHTTPServer start/stop lifecycle."""

    def test_bound_port_before_start_raises(self, registry: MetricsRegistry) -> None:
        srv = MetricsHTTPServer(registry, port=0)
        with pytest.raises(RuntimeError, match="not been started"):
            _ = srv.bound_port

    def test_start_assigns_ephemeral_port(self, registry: MetricsRegistry) -> None:
        srv = MetricsHTTPServer(registry, port=0)
        srv.start()
        try:
            assert srv.bound_port > 0
        finally:
            srv.stop()

    def test_double_start_raises(self, server: MetricsHTTPServer) -> None:
        with pytest.raises(RuntimeError, match="already started"):
            server.start()

    def test_stop_without_start_is_noop(self, registry: MetricsRegistry) -> None:
        srv = MetricsHTTPServer(registry, port=0)
        srv.stop()  # must not raise

    def test_stop_releases_port_for_immediate_rebind(self, registry: MetricsRegistry) -> None:
        srv = MetricsHTTPServer(registry, host="127.0.0.1", port=0)
        srv.start()
        port = srv.bound_port
        srv.stop()

        rebound = MetricsHTTPServer(registry, host="127.0.0.1", port=port)
        rebound.start()
        try:
            assert rebound.bound_port == port
        finally:
            rebound.stop()


class TestMetricsEndpoint:
    """Tests for GET /metrics."""

    def test_metrics_endpoint_returns_200_and_content_type(self, server: MetricsHTTPServer) -> None:
        status, headers, _ = _get(server, "/metrics")
        assert status == 200
        assert headers["Content-Type"].startswith("text/plain")

    def test_metrics_endpoint_reflects_registry_state(
        self, server: MetricsHTTPServer, registry: MetricsRegistry
    ) -> None:
        registry.counter("requests_total", "total requests").inc(3.0)
        _, _, body = _get(server, "/metrics")
        text = body.decode("utf-8")
        assert "# HELP requests_total total requests" in text
        assert "requests_total 3.0" in text

    def test_metrics_endpoint_empty_registry(self, server: MetricsHTTPServer) -> None:
        status, _, body = _get(server, "/metrics")
        assert status == 200
        assert body == b""


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_endpoint_default_ok(self, server: MetricsHTTPServer) -> None:
        status, headers, body = _get(server, "/health")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        assert json.loads(body) == {"status": "ok"}

    def test_health_endpoint_uses_provider(self, registry: MetricsRegistry) -> None:
        srv = MetricsHTTPServer(
            registry, port=0, health_provider=lambda: {"state": "connected", "uptime_seconds": 42}
        )
        srv.start()
        try:
            _, _, body = _get(srv, "/health")
            assert json.loads(body) == {"state": "connected", "uptime_seconds": 42}
        finally:
            srv.stop()


class TestUnknownPath:
    """Tests for requests to unmapped paths."""

    def test_unknown_path_returns_404(self, server: MetricsHTTPServer) -> None:
        status, _, _ = _get(server, "/nope")
        assert status == 404
