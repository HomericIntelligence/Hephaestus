"""NATS connection configuration.

Provides the :class:`NATSConfig` Pydantic model and a loader function that
reads from a YAML dict with optional environment variable overrides.

Usage::

    from hephaestus.nats.config import NATSConfig, load_nats_config

    config = NATSConfig(enabled=True, url="nats://localhost:4222")
    # or load from YAML:
    config = load_nats_config(yaml_dict["nats"])
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Valid JetStream first-subscription deliver policies. These string values match
# nats.js.api.DeliverPolicy's enum values, so the subscriber can build the enum
# directly from the configured string (see hephaestus/nats/subscriber.py).
DeliverPolicyStr = Literal[
    "all", "last", "new", "by_start_sequence", "by_start_time", "last_per_subject"
]


class NATSConfig(BaseModel):
    """NATS JetStream connection configuration.

    Attributes:
        enabled: Whether NATS subscription is active.
        url: NATS server URL.
        stream: JetStream stream name.
        subjects: Subject patterns to subscribe to.
        durable_name: Durable consumer name for at-least-once delivery.
        deliver_policy: JetStream deliver policy for first-time subscription.
        initial_backoff_seconds: Initial wait before the first reconnect attempt.
        max_backoff_seconds: Upper bound for exponential reconnect backoff.
        backoff_multiplier: Multiplier applied to backoff after each reconnect.

    """

    enabled: bool = Field(default=False, description="Enable NATS event subscription")
    url: str = Field(default="nats://localhost:4222", description="NATS server URL")
    stream: str = Field(default="TASKS", description="JetStream stream name")
    subjects: list[str] = Field(
        default_factory=list,
        description="Subject patterns to subscribe to",
    )
    durable_name: str = Field(
        default="hephaestus-subscriber",
        description="Durable consumer name for at-least-once delivery",
    )
    deliver_policy: DeliverPolicyStr = Field(
        default="new",
        description="JetStream deliver policy (new, all, last, etc.)",
    )
    initial_backoff_seconds: float = Field(
        default=1.0,
        gt=0.0,
        description="Initial wait (seconds) before the first reconnect attempt.",
    )
    max_backoff_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description="Upper bound (seconds) for exponential reconnect backoff.",
    )
    backoff_multiplier: float = Field(
        default=2.0,
        gt=1.0,
        description="Multiplier applied to the current backoff after each failed reconnect.",
    )

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> NATSConfig:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be >= initial_backoff_seconds "
                f"(got max={self.max_backoff_seconds}, initial={self.initial_backoff_seconds})"
            )
        return self


def load_nats_config(
    yaml_config: dict[str, Any],
    env_override: bool = True,
) -> NATSConfig:
    """Load NATS configuration from a YAML dict with optional env var overrides.

    The following environment variables are applied when *env_override* is
    ``True``:

    - ``NATS_URL`` overrides ``url``
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
        env_url = os.environ.get("NATS_URL")
        if env_url:
            data["url"] = env_url

        env_stream = os.environ.get("NATS_STREAM")
        if env_stream:
            data["stream"] = env_stream

        env_durable = os.environ.get("NATS_DURABLE_NAME")
        if env_durable:
            data["durable_name"] = env_durable

        env_initial = os.environ.get("NATS_INITIAL_BACKOFF_SECONDS")
        if env_initial is not None:
            data["initial_backoff_seconds"] = float(env_initial)

        env_max = os.environ.get("NATS_MAX_BACKOFF_SECONDS")
        if env_max is not None:
            data["max_backoff_seconds"] = float(env_max)

        env_mult = os.environ.get("NATS_BACKOFF_MULTIPLIER")
        if env_mult is not None:
            data["backoff_multiplier"] = float(env_mult)

    return NATSConfig(**data)
