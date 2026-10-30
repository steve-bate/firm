"""Retries due deliveries, in this process, on a fixed tick."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from firm.core.delivery.queue import DeliveryQueue
from firm.server.config import DeliveryConfig
from firm.server.delivery import FirmDeliveryService

log = logging.getLogger(__name__)


class DeliveryWorker:
    """Takes due units of work off the queue and attempts them."""

    def __init__(
        self,
        service: FirmDeliveryService,
        queue: DeliveryQueue,
        config: DeliveryConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._service = service
        self._queue = queue
        self._config = config
        self._clock = clock

    async def tick(self) -> int:
        """One pass: recover dead claims, attempt what is due, forget old history."""
        now = self._clock()
        await self._queue.reclaim_stale(now=now, lease=self._config.lease)
        due = await self._queue.claim_due(now=now, limit=self._config.batch)
        for delivery in due:
            await self._service.run(delivery)
        await self._queue.prune(now=now, history_ttl=self._config.history_ttl)
        return len(due)

    async def serve(self) -> None:
        """Tick until cancelled."""
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Delivery worker tick failed")
            await asyncio.sleep(self._config.worker_interval)
