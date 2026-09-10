"""Run the review queue command."""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Delegate the command to the shared queue boundary."""
    from .pipeline_cli import main as run_queue

    return run_queue(argv, profile="review")


if __name__ == "__main__":
    raise SystemExit(main())
