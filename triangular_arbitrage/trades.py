from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from triangular_arbitrage.models import Opportunity


@dataclass(frozen=True, slots=True)
class TradeRecord:
    ts: float
    exchange: str
    start_asset: str
    start_amount: float
    end_amount: float
    net_profit: float
    net_profit_bps: float
    path: str

    def short_line(self) -> str:
        t = time.strftime("%m-%d %H:%M:%S", time.localtime(self.ts))
        sign = "+" if self.net_profit >= 0 else ""
        return (
            f"{t} {self.exchange} {sign}{self.net_profit:.4f} "
            f"({self.net_profit_bps:.1f}bps) "
            f"{self.start_amount:.2f}->{self.end_amount:.2f} {self.start_asset}"
        )


@dataclass
class TradeBook:
    max_trades: int = 50
    trades: deque[TradeRecord] = field(default_factory=lambda: deque(maxlen=50))
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.trades = deque(maxlen=self.max_trades)

    def record(self, exchange: str, opportunity: Opportunity) -> TradeRecord:
        path = " → ".join(
            f"{hop.side[0].upper()} {hop.market}" for hop in opportunity.hops
        )
        row = TradeRecord(
            ts=time.time(),
            exchange=exchange,
            start_asset=opportunity.cycle.start_asset,
            start_amount=opportunity.start_amount,
            end_amount=opportunity.end_amount,
            net_profit=opportunity.net_profit,
            net_profit_bps=opportunity.net_profit_bps,
            path=path,
        )
        self.trades.appendleft(row)
        key = exchange.lower()
        self.counts[key] = self.counts.get(key, 0) + 1
        return row

    def seed(self, records: list[TradeRecord]) -> None:
        """Restore past trades (oldest first) for /trades; session counts stay untouched."""
        for row in records:
            self.trades.appendleft(row)

    def recent(self, limit: int = 10) -> list[TradeRecord]:
        return list(self.trades)[:limit]

    def count_for(self, exchange: str) -> int:
        return self.counts.get(exchange.lower(), 0)

    def total_count(self) -> int:
        return sum(self.counts.values())
