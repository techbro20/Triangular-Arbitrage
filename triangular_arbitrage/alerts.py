from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("arb.alerts")

AlertHandler = Callable[[str], Awaitable[None]]


class AlertHub:
    def __init__(self) -> None:
        self._handlers: list[AlertHandler] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def add_handler(self, handler: AlertHandler) -> None:
        self._handlers.append(handler)

    async def send(self, message: str) -> None:
        await self._queue.put(message)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            log.info("%s", message.replace("\n", " | "))
            for handler in self._handlers:
                try:
                    await handler(message)
                except Exception:
                    log.exception("alert handler failed")
