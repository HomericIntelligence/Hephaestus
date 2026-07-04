"""NATS connection configuration.

Provides the :class:`NATSConfig` dataclass and a loader function that
reads from a YAML dict with optional environment variable overrides.

Usage::

    from hephaestus.nats.config import NATSConfig, load_nats_config

    config = NATSConfig(enabled=True, url="tls://nats.example.com:4222")
    # or load from YAML:
    config = load_nats_config(yaml_dict["nats"])
"""

from __future__ import annotations

import ipaddress
import os
import ssl
from dataclasses import dataclass, field as dataclass_field, fields as dataclass_fields
from typing import Any, Literal
from urllib.parse import urlparse

# Valid JetStream first-subscription deliver policies. These string values match
# nats.js.api.DeliverPolicy's enum values, so the subscriber can build the enum
# directly from the configured string (see hephaestus/nats/subscriber.py).
DeliverPolicyStr = Literal[
    "all", "last", "new", "by_start_sequence", "by_start_time", "last_per_subject"
]

# Runtime allowlist mirroring DeliverPolicyStr. pydantic enforced the Literal at
# construction; a stdlib dataclass does not, so __post_init__ checks membership
# explicitly (subscriber.py builds a DeliverPolicy enum from this string).
_DELIVER_POLICIES = frozenset(
    {"all", "last", "new", "by_start_sequence", "by_start_time", "last_per_subject"}
)
_FLOAT_FIELDS = frozenset({"initial_backoff_seconds", "max_backoff_seconds", "backoff_multiplier"})


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
        deliver_policy: JetStream deliver policy for first-time subscription.
        initial_backoff_seconds: Initial wait before the first reconnect attempt.
        max_backoff_seconds: Upper bound for exponential reconnect backoff.
        backoff_multiplier: Multiplier applied to backoff after each reconnect.

    Environment variables (read by :meth:`from_env` and by
    :func:`load_nats_config` when ``env_override=True``):

    - ``NATS_URL`` → ``url`` (str)
    - ``NATS_TLS`` → ``tls`` (bool)
    - ``NATS_TLS_CA_FILE`` → ``tls_ca_file`` (str)
    - ``NATS_TLS_CERT_FILE`` → ``tls_cert_file`` (str)
    - ``NATS_TLS_KEY_FILE`` → ``tls_key_file`` (str)
    - ``NATS_TLS_HOSTNAME`` → ``tls_hostname`` (str)
    - ``NATS_TLS_HANDSHAKE_FIRST`` → ``tls_handshake_first`` (bool)
    - ``NATS_ALLOW_PLAINTEXT`` → ``allow_plaintext`` (bool)
    - ``NATS_STREAM`` → ``stream`` (str)
    - ``NATS_DURABLE_NAME`` → ``durable_name`` (str)
    - ``NATS_INITIAL_BACKOFF_SECONDS`` → ``initial_backoff_seconds`` (float > 0)
    - ``NATS_MAX_BACKOFF_SECONDS`` → ``max_backoff_seconds`` (float > 0)
    - ``NATS_BACKOFF_MULTIPLIER`` → ``backoff_multiplier`` (float > 1)

    ``enabled``, ``subjects``, and ``deliver_policy`` are not env-configurable
    and must be set via the constructor or YAML.

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
    deliver_policy: DeliverPolicyStr = "new"
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    backoff_multiplier: float = 2.0

    def __post_init__(self) -> None:
        """Validate deliver-policy membership and backoff bounds.

        Raises:
            ValueError: If ``deliver_policy`` is not a known policy, a backoff
                field is out of range, or ``max_backoff_seconds`` is below
                ``initial_backoff_seconds``.

        """
        if self.deliver_policy not in _DELIVER_POLICIES:
            raise ValueError(
                f"deliver_policy must be one of {sorted(_DELIVER_POLICIES)}, "
                f"got {self.deliver_policy!r}"
            )
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
        if self.tls_key_file and not self.tls_cert_file:
            raise ValueError("tls_key_file requires tls_cert_file")
        if (
            self.enabled
            and not self.tls_enabled
            and _is_nonlocal_plaintext_url(self.url)
            and not self.allow_plaintext
        ):
            raise ValueError(
                "enabled NATS plaintext nats:// URLs are allowed only for localhost; "
                "use tls:// or tls=True for production, or set allow_plaintext=True "
                "for an explicit non-production exception"
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

    @classmethod
    def from_env(cls, **overrides: Any) -> NATSConfig:
        """Build a :class:`NATSConfig` from ``NATS_*`` environment variables.

        Reads the ``NATS_*`` variables documented on this class. Keyword
        ``overrides`` are applied first (acting as defaults/base values) and
        any matching environment variable then overrides them, mirroring
        :func:`load_nats_config`.

        Args:
            **overrides: Base field values applied before env vars are read.

        Returns:
            Validated :class:`NATSConfig` instance.

        Raises:
            ValueError: If a numeric env var is not a valid number, or if the
                resulting backoff bounds are invalid.

        """
        data = _apply_env_overrides(dict(overrides))
        return cls(**data)


def _coerce_float(name: str, raw: Any) -> float:
    """Coerce an env var string to ``float`` with a variable-named error.

    Args:
        name: Environment variable name (used in the error message).
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
    """Coerce an env var string to ``bool`` with a variable-named error."""
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _is_nonlocal_plaintext_url(url: str) -> bool:
    """Return whether *url* is a plaintext NATS URL for a non-loopback host."""
    parsed = urlparse(url)
    if parsed.scheme != "nats":
        return False
    host = parsed.hostname
    if host is None or host in {"", "localhost"}:
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply ``NATS_*`` env var overrides onto *data* in place and return it.

    String vars use a truthy guard (an empty value is ignored); numeric and
    boolean vars use an ``is not None`` guard (an explicit empty value raises).

    Args:
        data: Field-value mapping to overlay env vars onto.

    Returns:
        The same *data* dict, mutated with any present env-var overrides.

    Raises:
        ValueError: If a numeric or boolean env var is invalid.

    """
    str_vars = {
        "NATS_URL": "url",
        "NATS_TLS_CA_FILE": "tls_ca_file",
        "NATS_TLS_CERT_FILE": "tls_cert_file",
        "NATS_TLS_KEY_FILE": "tls_key_file",
        "NATS_TLS_HOSTNAME": "tls_hostname",
        "NATS_STREAM": "stream",
        "NATS_DURABLE_NAME": "durable_name",
    }
    for env_name, field in str_vars.items():
        value = os.environ.get(env_name)
        if value:
            data[field] = value

    float_vars = {
        "NATS_INITIAL_BACKOFF_SECONDS": "initial_backoff_seconds",
        "NATS_MAX_BACKOFF_SECONDS": "max_backoff_seconds",
        "NATS_BACKOFF_MULTIPLIER": "backoff_multiplier",
    }
    for env_name, field in float_vars.items():
        raw = os.environ.get(env_name)
        if raw is not None:
            data[field] = _coerce_float(env_name, raw)

    bool_vars = {
        "NATS_TLS": "tls",
        "NATS_TLS_HANDSHAKE_FIRST": "tls_handshake_first",
        "NATS_ALLOW_PLAINTEXT": "allow_plaintext",
    }
    for env_name, field in bool_vars.items():
        raw = os.environ.get(env_name)
        if raw is not None:
            data[field] = _coerce_bool(env_name, raw)

    return data


def load_nats_config(
    yaml_config: dict[str, Any],
    env_override: bool = True,
) -> NATSConfig:
    """Load NATS configuration from a YAML dict with optional env var overrides.

    The following environment variables are applied when *env_override* is
    ``True``:

    - ``NATS_URL`` overrides ``url``
    - ``NATS_TLS`` overrides ``tls``
    - ``NATS_TLS_CA_FILE`` overrides ``tls_ca_file``
    - ``NATS_TLS_CERT_FILE`` overrides ``tls_cert_file``
    - ``NATS_TLS_KEY_FILE`` overrides ``tls_key_file``
    - ``NATS_TLS_HOSTNAME`` overrides ``tls_hostname``
    - ``NATS_TLS_HANDSHAKE_FIRST`` overrides ``tls_handshake_first``
    - ``NATS_ALLOW_PLAINTEXT`` overrides ``allow_plaintext``
    - ``NATS_STREAM`` overrides ``stream``
    - ``NATS_DURABLE_NAME`` overrides ``durable_name``
    - ``NATS_INITIAL_BACKOFF_SECONDS`` overrides ``initial_backoff_seconds``
    - ``NATS_MAX_BACKOFF_SECONDS`` overrides ``max_backoff_seconds``
    - ``NATS_BACKOFF_MULTIPLIER`` overrides ``backoff_multiplier``

    Args:
        yaml_config: Parsed YAML section for the NATS block.
        env_override: Whether to apply environment variable overrides.

    Returns:
        Validated :class:`NATSConfig` instance.

    """
    data: dict[str, Any] = dict(yaml_config)

    if env_override:
        data = _apply_env_overrides(data)

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
    return NATSConfig(**filtered)
