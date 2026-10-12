from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

BookSide = Literal["bids", "asks"]


@dataclass
class LocalOrderBook:
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    ts: float = 0.0
    exchange_ts: float | None = None
    version: int | None = None
    # Paper fills never reach the exchange, so the next snapshot would re-show liquidity
    # we already took. price -> (qty, expires_at); survives snapshots until expiry.
    consumed_bids: dict[float, tuple[float, float]] = field(default_factory=dict)
    consumed_asks: dict[float, tuple[float, float]] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.bids and self.asks)

    def apply_snapshot(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        ts: float | None = None,
        *,
        exchange_ts: float | None = None,
        version: int | None = None,
    ) -> bool:
        """Replace the book. Returns False (and keeps the old book) for out-of-order versions."""
        if version is not None and self.version is not None and version < self.version:
            return False
        self.bids = sorted(
            ((float(p), float(q)) for p, q in bids if float(q) > 0),
            key=lambda level: -level[0],
        )
        self.asks = sorted(
            ((float(p), float(q)) for p, q in asks if float(q) > 0),
            key=lambda level: level[0],
        )
        self.ts = ts if ts is not None else time.time()
        self.exchange_ts = exchange_ts
        if version is not None:
            self.version = version
        return True

    def invalidate(self) -> None:
        self.bids = []
        self.asks = []
        self.ts = 0.0
        self.exchange_ts = None
        self.version = None

    def _effective(
        self,
        levels: list[tuple[float, float]],
        consumed: dict[float, tuple[float, float]],
    ) -> list[tuple[float, float]]:
        if not consumed:
            return levels
        now = time.time()
        for price in [p for p, (_q, exp) in consumed.items() if exp <= now]:
            del consumed[price]
        if not consumed:
            return levels
        out: list[tuple[float, float]] = []
        for price, qty in levels:
            used = consumed.get(price)
            left = qty - used[0] if used else qty
            if left > 1e-12:
                out.append((price, left))
        return out

    def effective_bids(self) -> list[tuple[float, float]]:
        return self._effective(self.bids, self.consumed_bids)

    def effective_asks(self) -> list[tuple[float, float]]:
        return self._effective(self.asks, self.consumed_asks)

    def consume(self, side: BookSide, taken: Iterable[tuple[float, float]], hold_seconds: float) -> None:
        if hold_seconds <= 0:
            return
        consumed = self.consumed_bids if side == "bids" else self.consumed_asks
        expires = time.time() + hold_seconds
        for price, qty in taken:
            if qty <= 0:
                continue
            prev_qty = consumed.get(price, (0.0, 0.0))[0]
            consumed[price] = (prev_qty + qty, expires)

    def best_bid(self) -> float | None:
        levels = self.effective_bids()
        return levels[0][0] if levels else None

    def best_ask(self) -> float | None:
        levels = self.effective_asks()
        return levels[0][0] if levels else None

    def is_crossed(self) -> bool:
        return bool(self.bids and self.asks and self.bids[0][0] >= self.asks[0][0])

    def age_seconds(self) -> float:
        if not self.ts:
            return float("inf")
        return time.time() - self.ts

    def feed_lag_seconds(self) -> float | None:
        """Receive time minus exchange time for the latest message (None if unknown)."""
        if self.exchange_ts is None or not self.ts:
            return None
        return self.ts - self.exchange_ts


class BookStore:
    def __init__(self) -> None:
        self._books: dict[str, LocalOrderBook] = {}

    def get(self, unified: str) -> LocalOrderBook | None:
        return self._books.get(unified)

    def ensure(self, unified: str) -> LocalOrderBook:
        book = self._books.get(unified)
        if book is None:
            book = LocalOrderBook()
            self._books[unified] = book
        return book

    def apply_snapshot(
        self,
        unified: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        ts: float | None = None,
        *,
        exchange_ts: float | None = None,
        version: int | None = None,
    ) -> bool:
        book = self.ensure(unified)
        return book.apply_snapshot(bids, asks, ts, exchange_ts=exchange_ts, version=version)

    def consume(
        self,
        unified: str,
        side: BookSide,
        taken: Iterable[tuple[float, float]],
        hold_seconds: float,
    ) -> None:
        book = self._books.get(unified)
        if book is not None:
            book.consume(side, taken, hold_seconds)

    def invalidate(self, unified: str) -> None:
        book = self._books.get(unified)
        if book is not None:
            book.invalidate()

    def invalidate_many(self, unifieds: list[str] | set[str]) -> None:
        for unified in unifieds:
            self.invalidate(unified)

    def ready_count(self) -> int:
        return sum(1 for book in self._books.values() if book.ready)

    def __len__(self) -> int:
        return len(self._books)
