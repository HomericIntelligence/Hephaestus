"""``hephaestus-mesh-worker`` — run one mesh worker for a (domain, role) queue.

Configuration comes from the environment (see :class:`MeshConfig.from_env`):
``MESH_DOMAIN``/``MESH_ROLE`` select the queue and handler; ``NATS_URL``,
``NATS_CLIENT_TOKEN``, ``NATS_CA_FILE``, ``AGAMEMNON_URL``,
``AGAMEMNON_API_KEY`` wire the transports. AchaeanFleet's ``achaean-mesh``
vessel sets these per compose service.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from hephaestus.automation.mesh.config import MeshConfig
from hephaestus.automation.mesh.roles import resolve_handler
from hephaestus.automation.mesh.worker import MeshWorker
from hephaestus.cli.utils import add_json_arg, add_version_arg, emit_json_status


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser (env vars remain the primary configuration)."""
    parser = argparse.ArgumentParser(
        prog="hephaestus-mesh-worker",
        description="Serve one HMAS mesh (domain, role) work queue (ADR-013).",
    )
    parser.add_argument("--domain", help="Override MESH_DOMAIN")
    parser.add_argument("--role", help="Override MESH_ROLE")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    add_version_arg(parser)
    add_json_arg(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: build config + handler and run the claim loop."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import os

    if args.domain:
        os.environ["MESH_DOMAIN"] = args.domain
    if args.role:
        os.environ["MESH_ROLE"] = args.role
    try:
        config = MeshConfig.from_env()
    except KeyError as exc:
        if args.json:
            emit_json_status(2, message=f"missing environment variable: {exc}")
        else:
            print(f"missing required environment variable: {exc}", file=sys.stderr)
        return 2
    try:
        handler = resolve_handler(config.domain, config.role)
    except KeyError as exc:
        if args.json:
            emit_json_status(2, message=str(exc))
        else:
            print(str(exc), file=sys.stderr)
        return 2

    worker = MeshWorker(config, handler)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(worker.run_forever())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
