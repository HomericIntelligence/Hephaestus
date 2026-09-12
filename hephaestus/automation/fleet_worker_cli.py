"""Expose the worker through a private allocation-local Unix socket."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import sys
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_request_evidence import read_request_evidence
from hephaestus.automation.fleet_worker import FleetWorker
from hephaestus.cli.utils import add_json_arg, add_version_arg

_MAX_MESSAGE = 1024 * 1024


def exchange(state_dir: Path, message: dict[str, Any]) -> dict[str, Any]:
    """Send one bounded request through the private local worker socket."""
    data = (json.dumps(message) + "\n").encode()
    if len(data) > _MAX_MESSAGE:
        raise ValueError("message_limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(40)
        connection.connect(str(state_dir / "worker.sock"))
        connection.sendall(data)
        with connection.makefile("rb") as stream:
            response = stream.readline(_MAX_MESSAGE + 1)
        if not response.endswith(b"\n") or len(response) > _MAX_MESSAGE:
            raise ValueError("invalid_response")
        result = json.loads(response)
        if not isinstance(result, dict):
            raise ValueError("invalid_response")
        return result


def _dispatch(worker: FleetWorker, message: dict[str, Any]) -> dict[str, Any]:
    operation = message.get("operation")
    if operation == "inventory":
        return worker.inventory()
    if operation == "events":
        return worker.events(message.get("after", 0), limit=message.get("limit", 500))
    if operation == "requests":
        # Request details belong only on the authenticated private attachment.
        return {
            "requests": [
                item
                for item in worker.pending.values()
                if item["sessionId"] == message.get("targetId")
            ]
        }
    if operation == "request-evidence":
        target_id, request_id = message.get("targetId"), message.get("requestId")
        if (
            not isinstance(target_id, str)
            or not isinstance(request_id, (str, int))
            or isinstance(request_id, bool)
        ):
            raise ValueError("invalid_request")
        return read_request_evidence(worker, target_id, request_id)
    return worker.handle(message)


def serve(worker: FleetWorker) -> None:
    """Process control requests serially while model turns run concurrently."""
    socket_path = worker.journal.directory / "worker.sock"
    socket_path.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        """Serve one framed command on an allocation-local attachment."""

        def handle(self) -> None:
            """Read and reply to one bounded command."""
            self.request.settimeout(5)
            try:
                data = self.rfile.readline(_MAX_MESSAGE + 1)
                if not data.endswith(b"\n") or len(data) > _MAX_MESSAGE:
                    raise ValueError("message_limit")
                message = json.loads(data)
                if not isinstance(message, dict):
                    raise ValueError("invalid_message")
                result = _dispatch(worker, message)
            except (ValueError, OSError):
                result = {"error": "invalid_request"}
            self.wfile.write((json.dumps(result) + "\n").encode())

    stopped = False

    def stop(_number: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
            os.chmod(socket_path, 0o600)
            server.timeout = 0.2
            while not stopped:
                server.handle_request()
                worker.poll()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        socket_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run a private worker or attach through an existing authenticated transport."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_json_arg(parser)
    add_version_arg(parser)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    serving = subcommands.add_parser("serve")
    for name in ("state-dir", "workspace-root", "codex-home"):
        serving.add_argument(f"--{name}", required=True, type=Path)
    for name in ("worker-id", "pool-id", "host-id"):
        serving.add_argument(f"--{name}", required=True)
    for name in ("capacity", "generation"):
        serving.add_argument(f"--{name}", required=True, type=int)
    serving.add_argument("--allocation-id")
    serving.add_argument("--codex-bin", default="codex")
    for operation in ("attach", "inventory", "events"):
        command = subcommands.add_parser(operation)
        command.add_argument("--state-dir", required=True, type=Path)
        if operation == "events":
            command.add_argument("--after", type=int, default=0)
    args = parser.parse_args(argv)
    if args.operation == "serve":
        options = vars(args).copy()
        options.pop("operation")
        options.pop("json")
        options["provider_command"] = [options.pop("codex_bin")]
        worker = FleetWorker(**options)
        try:
            worker.start()
            serve(worker)
        finally:
            worker.close()
    elif args.operation == "attach":
        for line in sys.stdin:
            message = json.loads(line)
            print(json.dumps(exchange(args.state_dir, message)), flush=True)
    else:
        print(
            json.dumps(
                exchange(
                    args.state_dir,
                    {"operation": args.operation, "after": getattr(args, "after", 0)},
                )
            )
        )
    return 0
