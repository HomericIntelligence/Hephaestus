"""NATS connection configuration.

Provides the :class:`NATSConfig` dataclass and a loader function that
reads from an explicit YAML mapping.

Usage::

    from hephaestus.nats.config import NATSConfig, load_nats_config

    config = NATSConfig(enabled=True, url="tls://nats.example.com:4222")
    # or load from YAML:
    config = load_nats_config(yaml_dict["nats"])
"""

from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass, field as dataclass_field, fields as dataclass_fields
from typing import Any
from urllib.parse import urlparse

from hephaestus.nats.failure_models import MAX_FAILURE_PAYLOAD_BYTES

_FLOAT_FIELDS = frozenset(
    {
        "initial_backoff_seconds",
        "max_backoff_seconds",
        "backoff_multiplier",
        "ack_wait_seconds",
        "heartbeat_interval_seconds",
        "ledger_lease_seconds",
    }
)
_BOOL_FIELDS = frozenset(
    {
        "tls",
        "tls_handshake_first",
        "allow_plaintext",
        "auto_provision_failure_resources",
        "migrate_incompatible_consumers",
    }
)
_INT_FIELDS = frozenset({"max_processing_attempts", "max_deliveries", "max_failure_payload_bytes"})


@dataclass
class NATSConfig:
    """NATS JetStream connection configuration.

    Attributes:
        enabled: Whether NATS subscription is active.
        url: NATS server URL.
        tls: Build and pass an SSLContext to nats-py.
        tls_ca_file: CA bundle used to verify the NATS server certificate.
        tls_cert_file: Client certificate file for mTLS.
        tls_key_file: Client private key file for mTLS.
        tls_hostname: Hostname override for certificate verification.
        tls_handshake_first: Start TLS before the NATS INFO protocol handshake.
        allow_plaintext: Permit non-local plaintext nats:// URLs.
        stream: JetStream stream name.
        subjects: Subject patterns to subscribe to.
        durable_name: Durable consumer name for at-least-once delivery.
        initial_backoff_seconds: Initial wait before the first reconnect attempt.
        max_backoff_seconds: Upper bound for exponential reconnect backoff.
        backoff_multiplier: Multiplier applied to backoff after each reconnect.
        max_processing_attempts: Processing attempts before a message hands off
            to durable failure storage.
        max_deliveries: Physical JetStream deliveries; must exceed
            ``max_processing_attempts`` so handoff retries run on deliveries
            beyond the processing budget.
        redelivery_backoff_seconds: NAK delays applied on known failures below
            the processing budget.
        ack_wait_seconds: Broker redelivery wait for the explicit-ack consumer.
        heartbeat_interval_seconds: Interval for ``in_progress()`` heartbeats
            and delivery-lease renewal while the handler runs.
        ledger_lease_seconds: Delivery-ledger lease duration.
        max_failure_payload_bytes: Captured payload size bound; must not exceed
            the broker-provisioned dead-letter payload boundary.
        auto_provision_failure_resources: Provision missing failure resources
            non-destructively at startup. ``False`` is a read-only
            validation/open mode.
        migrate_incompatible_consumers: Opt-in incompatible durable-consumer
            cutover. Default startup fails closed instead.

    All fields are supplied through the constructor or an explicit YAML mapping.

    """

    enabled: bool = False
    url: str = "tls://localhost:4222"
    tls: bool = True
    tls_ca_file: str | None = None
    tls_cert_file: str | None = None
    tls_key_file: str | None = None
    tls_hostname: str | None = None
    tls_handshake_first: bool = False
    allow_plaintext: bool = False
    stream: str = "TASKS"
    subjects: list[str] = dataclass_field(default_factory=list)
    durable_name: str = "hephaestus-subscriber"
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    backoff_multiplier: float = 2.0
    max_processing_attempts: int = 3
    max_deliveries: int = 6
    redelivery_backoff_seconds: tuple[float, ...] = (1.0, 5.0)
    ack_wait_seconds: float = 30.0
    heartbeat_interval_seconds: float = 10.0
    ledger_lease_seconds: float = 30.0
    max_failure_payload_bytes: int = MAX_FAILURE_PAYLOAD_BYTES
    auto_provision_failure_resources: bool = True
    migrate_incompatible_consumers: bool = False

    def __post_init__(self) -> None:
        """Validate backoff, delivery, and failure-resource bounds.

        Raises:
            ValueError: If a backoff field is out of range,
                ``max_backoff_seconds`` is below ``initial_backoff_seconds``,
                or the durable failure-delivery fields violate their bounds.

        """
        self.tls = _coerce_config_bool("tls", self.tls)
        self.tls_handshake_first = _coerce_config_bool(
            "tls_handshake_first",
            self.tls_handshake_first,
        )
        self.allow_plaintext = _coerce_config_bool("allow_plaintext", self.allow_plaintext)
        self.auto_provision_failure_resources = _coerce_config_bool(
            "auto_provision_failure_resources",
            self.auto_provision_failure_resources,
        )
        self.migrate_incompatible_consumers = _coerce_config_bool(
            "migrate_incompatible_consumers",
            self.migrate_incompatible_consumers,
        )
        self.max_processing_attempts = _coerce_config_int(
            "max_processing_attempts",
            self.max_processing_attempts,
        )
        self.max_deliveries = _coerce_config_int("max_deliveries", self.max_deliveries)
        self.max_failure_payload_bytes = _coerce_config_int(
            "max_failure_payload_bytes",
            self.max_failure_payload_bytes,
        )
        self.redelivery_backoff_seconds = _coerce_backoff_delays(self.redelivery_backoff_seconds)

        if self.initial_backoff_seconds <= 0.0:
            raise ValueError(
                f"initial_backoff_seconds must be > 0, got {self.initial_backoff_seconds}"
            )
        if self.max_backoff_seconds <= 0.0:
            raise ValueError(f"max_backoff_seconds must be > 0, got {self.max_backoff_seconds}")
        if self.backoff_multiplier <= 1.0:
            raise ValueError(f"backoff_multiplier must be > 1, got {self.backoff_multiplier}")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be >= initial_backoff_seconds "
                f"(got max={self.max_backoff_seconds}, initial={self.initial_backoff_seconds})"
            )
        self._validate_failure_delivery()
        if self.tls_key_file and not self.tls_cert_file:
            raise ValueError("tls_key_file requires tls_cert_file")
        if (
            self.enabled
            and not self.tls_enabled
            and _is_nonlocal_plaintext_url(self.url)
            and not self.allow_plaintext
        ):
            scheme = urlparse(self.url).scheme or "nats"
            raise ValueError(
                f"enabled NATS plaintext {scheme}:// URLs are allowed only for localhost; "
                "use tls:// or tls=True for production, or set allow_plaintext=True "
                "for an explicit non-production exception"
            )

    def _validate_failure_delivery(self) -> None:
        """Validate the durable failure-delivery bounds (#2395).

        Raises:
            ValueError: If the attempt budget, delivery budget, ACK wait,
                heartbeat, lease, or payload bounds violate their rules.

        """
        if self.max_processing_attempts < 1:
            raise ValueError(
                f"max_processing_attempts must be >= 1, got {self.max_processing_attempts}"
            )
        if self.max_deliveries <= self.max_processing_attempts:
            raise ValueError(
                "max_deliveries must exceed max_processing_attempts "
                f"(got max_deliveries={self.max_deliveries}, "
                f"max_processing_attempts={self.max_processing_attempts})"
            )
        if self.ack_wait_seconds <= 0.0:
            raise ValueError(f"ack_wait_seconds must be > 0, got {self.ack_wait_seconds}")
        if self.heartbeat_interval_seconds <= 0.0:
            raise ValueError(
                f"heartbeat_interval_seconds must be > 0, got {self.heartbeat_interval_seconds}"
            )
        if self.heartbeat_interval_seconds > self.ack_wait_seconds / 3:
            raise ValueError(
                "heartbeat_interval_seconds must be <= ack_wait_seconds / 3 "
                f"(got heartbeat={self.heartbeat_interval_seconds}, "
                f"ack_wait={self.ack_wait_seconds})"
            )
        if self.ledger_lease_seconds < 2 * self.heartbeat_interval_seconds:
            raise ValueError(
                "ledger_lease_seconds must be >= 2 * heartbeat_interval_seconds "
                f"(got lease={self.ledger_lease_seconds}, "
                f"heartbeat={self.heartbeat_interval_seconds})"
            )
        if not 0 < self.max_failure_payload_bytes <= MAX_FAILURE_PAYLOAD_BYTES:
            raise ValueError(
                f"max_failure_payload_bytes must be between 1 and "
                f"{MAX_FAILURE_PAYLOAD_BYTES}, got {self.max_failure_payload_bytes}"
            )

    @property
    def tls_enabled(self) -> bool:
        """Return whether TLS options should be passed to nats-py."""
        return (
            self.tls
            or urlparse(self.url).scheme in {"tls", "wss"}
            or self.tls_ca_file is not None
            or self.tls_cert_file is not None
            or self.tls_key_file is not None
            or self.tls_hostname is not None
            or self.tls_handshake_first
        )

    def build_tls_context(self) -> ssl.SSLContext:
        """Build the SSL context passed to nats-py."""
        context = ssl.create_default_context(cafile=self.tls_ca_file)
        if self.tls_cert_file:
            context.load_cert_chain(certfile=self.tls_cert_file, keyfile=self.tls_key_file)
        return context

    def connect_options(self) -> dict[str, Any]:
        """Return keyword arguments for ``nats.connect``."""
        options: dict[str, Any] = {}
        if self.tls_enabled:
            options["tls"] = self.build_tls_context()
        if self.tls_hostname:
            options["tls_hostname"] = self.tls_hostname
        if self.tls_handshake_first:
            options["tls_handshake_first"] = True
        return options


def _coerce_float(name: str, raw: Any) -> float:
    """Coerce a configuration value to ``float`` with a field-named error.

    Args:
        name: Configuration field name used in the error message.
        raw: Raw value to coerce.

    Returns:
        The parsed float value.

    Raises:
        ValueError: If *raw* is not a valid float, naming *name*.

    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _coerce_bool(name: str, raw: str) -> bool:
    """Coerce a configuration string to ``bool`` with a field-named error."""
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _coerce_config_bool(field_name: str, raw: Any) -> bool:
    """Coerce a config bool field from bool/string input or reject it."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return _coerce_bool(field_name, raw)
    raise ValueError(f"{field_name} must be a boolean, got {raw!r}")


def _coerce_config_int(field_name: str, raw: Any) -> int:
    """Coerce a config int field from int/float/string input or reject it."""
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be an integer, got {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            pass
    raise ValueError(f"{field_name} must be an integer, got {raw!r}")


def _coerce_backoff_delays(raw: Any) -> tuple[float, ...]:
    """Coerce the redelivery backoff field to a tuple of positive floats."""
    if isinstance(raw, str):
        entries: list[Any] = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        entries = list(raw)
    else:
        raise ValueError(f"redelivery_backoff_seconds must be a sequence of numbers, got {raw!r}")
    if not entries:
        raise ValueError("redelivery_backoff_seconds must contain at least one delay")
    delays: list[float] = []
    for entry in entries:
        if isinstance(entry, bool):
            raise ValueError(f"redelivery_backoff_seconds entries must be numbers, got {entry!r}")
        try:
            delay = float(entry)
        except (TypeError, ValueError):
            raise ValueError(
                f"redelivery_backoff_seconds entries must be numbers, got {entry!r}"
            ) from None
        if delay <= 0.0:
            raise ValueError(f"redelivery_backoff_seconds entries must be > 0, got {delay}")
        delays.append(delay)
    return tuple(delays)


def _is_nonlocal_plaintext_url(url: str) -> bool:
    """Return whether *url* is a plaintext NATS/WebSocket URL for a non-loopback host."""
    parsed = urlparse(url)
    if parsed.scheme not in {"nats", "ws"}:
        return False
    host = parsed.hostname
    if host is None or host in {"", "localhost"}:
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def load_nats_config(yaml_config: dict[str, Any]) -> NATSConfig:
    """Load NATS configuration exclusively from an explicit YAML mapping."""
    data: dict[str, Any] = dict(yaml_config)

    # Pydantic's BaseModel silently dropped unknown keys; a stdlib dataclass
    # raises TypeError on them. Preserve the historical tolerant-YAML contract
    # (issue #1458): drop keys that are not NATSConfig fields so a typo'd or
    # forward-compatible config block still loads. Direct NATSConfig(...) calls
    # remain strict, which is the desired behavior for code callers.
    known = {f.name for f in dataclass_fields(NATSConfig)}
    filtered = {k: v for k, v in data.items() if k in known}
    for field_name in _FLOAT_FIELDS:
        if field_name in filtered:
            filtered[field_name] = _coerce_float(field_name, filtered[field_name])
    for field_name in _BOOL_FIELDS:
        if field_name in filtered:
            filtered[field_name] = _coerce_config_bool(field_name, filtered[field_name])
    for field_name in _INT_FIELDS:
        if field_name in filtered:
            filtered[field_name] = _coerce_config_int(field_name, filtered[field_name])
    return NATSConfig(**filtered)
