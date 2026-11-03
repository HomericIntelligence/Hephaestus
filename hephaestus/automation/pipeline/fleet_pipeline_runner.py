"""Public async entry for one admitted Fleet implementation through the normal coordinator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from hephaestus.automation.current_plan import read_current_plan

from .coordinator import _build_pipeline_coordinator
from .fleet_execution import (
    FleetAttempt,
    FleetController,
    FleetExecutor,
    PrivateWorkerExchange,
    validate_fleet_config,
)

if TYPE_CHECKING:
    from .coordinator_types import PipelineConfig


async def run_fleet_pipeline(
    config: PipelineConfig,
    *,
    client: FleetController,
    attempt: FleetAttempt,
    private_spool: Path,
    worker_exchange: PrivateWorkerExchange,
) -> int:
    """Run the normal coordinator with a caller-owned admitted Fleet connection."""
    executor = FleetExecutor(
        attempt,
        client=client,
        loop=asyncio.get_running_loop(),
        private_spool=private_spool,
        worker_exchange=worker_exchange,
        plan_reader=lambda: read_current_plan(
            attempt.issue,
            coordinator._ctx_for_repo(attempt.repository.rsplit("/", 1)[-1]).github,
        ),
    )
    validate_fleet_config(config, executor)
    coordinator = _build_pipeline_coordinator(
        config, fleet_executor=executor, install_signals=False
    )
    run = asyncio.create_task(asyncio.to_thread(coordinator.run))
    try:
        return await asyncio.shield(run)
    except asyncio.CancelledError:
        coordinator.shutdown_event.set()
        coordinator.worker_shutdown_event.set()
        coordinator._wake_completion_wait()
        await asyncio.shield(run)
        raise
