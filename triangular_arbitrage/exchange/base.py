from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from triangular_arbitrage.models import Market

# on_snapshot(unified, bids, asks, *, exchange_ts: float | None = None, version: int | None = None)
BookHandler = Callable[..., Awaitable[None]]
DisconnectHandler = Callable[[Sequence[str]], Awaitable[None]]


class ExchangeAdapter(Protocol):
    name: str

    async def load_markets(self) -> list[Market]:
        ...

    async def stream_books(
        self,
        native_by_unified: dict[str, str],
        on_snapshot: BookHandler,
        stop_event,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        ...
