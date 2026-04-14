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
from typing import Any

from pydantic import BaseModel, Field


class NATSConfig(BaseModel):
    """NATS JetStream connection configuration.

    Attributes:
        enabled: Whether NATS subscription is active.
        url: NATS server URL.
        stream: JetStream stream name.
        subjects: Subject patterns to subscribe to.
        durable_name: Durable consumer name for at-least-once delivery.
        deliver_policy: JetStream deliver policy for first-time subscription.

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
    deliver_policy: str = Field(
        default="new",
        description="JetStream deliver policy (new, all, last, etc.)",
    )


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

    return NATSConfig(**data)
