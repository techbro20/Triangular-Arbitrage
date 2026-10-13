from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from triangular_arbitrage.trades import TradeRecord

log = logging.getLogger("arb.history")

_WANTED = (
    '"status": "accepted"',
    '"status": "slipped"',
    '"status": "failed"',
    '"reason": "rest_mismatch"',
)


@dataclass
class ExchangeHistory:
    booked_pnl: float = 0.0
    booked_trades: int = 0
    slipped_pnl: float = 0.0
    slipped: int = 0
    failed: int = 0
    rest_mismatch: int = 0

    @property
    def honest_pnl(self) -> float:
        return self.booked_pnl + self.slipped_pnl


@dataclass
class PnlHistory:
    """All-time paper results, rebuilt from the opportunity log and updated live."""

    first_ts: float | None = None
    exchanges: dict[str, ExchangeHistory] = field(default_factory=dict)
    # exchange -> cycle id -> slipped/failed/REST mismatches in a row since its last held fill
    streaks: dict[str, dict[str, int]] = field(default_factory=dict)
    # exchange -> cycle id -> blocked until
    blocked: dict[str, dict[str, float]] = field(default_factory=dict)
    # exchange -> cycle id -> blocks so far since its last held fill (each doubles the next)
    block_levels: dict[str, dict[str, int]] = field(default_factory=dict)

    def get(self, exchange: str) -> ExchangeHistory:
        return self.exchanges.setdefault(exchange.lower(), ExchangeHistory())

    def _seen(self, ts: float) -> None:
        if self.first_ts is None or ts < self.first_ts:
            self.first_ts = ts

    def record_booked(self, exchange: str, cycle_id: str, profit: float, ts: float | None = None) -> None:
        self._seen(ts or time.time())
        row = self.get(exchange)
        row.booked_pnl += profit
        row.booked_trades += 1
        self.streaks.get(exchange.lower(), {}).pop(cycle_id, None)
        self.block_levels.get(exchange.lower(), {}).pop(cycle_id, None)

    def record_miss(
        self,
        exchange: str,
        cycle_id: str,
        status: str,
        profit: float | None,
        ts: float | None = None,
    ) -> int:
        """status: slipped / failed / rest_mismatch. Returns the cycle's miss streak."""
        self._seen(ts or time.time())
        row = self.get(exchange)
        if status == "slipped":
            row.slipped += 1
            if profit is not None:
                row.slipped_pnl += profit
        elif status == "rest_mismatch":
            row.rest_mismatch += 1
        else:
            row.failed += 1
        streaks = self.streaks.setdefault(exchange.lower(), {})
        streaks[cycle_id] = streaks.get(cycle_id, 0) + 1
        return streaks[cycle_id]

    def block(
        self,
        exchange: str,
        cycle_id: str,
        ts: float,
        base_hours: float,
        max_hours: float,
    ) -> tuple[float, float]:
        """Block with escalation (base, 2x, 4x, ... capped). Returns (until, hours)."""
        key = exchange.lower()
        levels = self.block_levels.setdefault(key, {})
        level = levels.get(cycle_id, 0)
        hours = base_hours * (2**level)
        if max_hours > 0:
            hours = min(hours, max_hours)
        until = ts + hours * 3600.0
        levels[cycle_id] = level + 1
        self.blocked.setdefault(key, {})[cycle_id] = until
        self.streaks.get(key, {}).pop(cycle_id, None)
        return until, hours

    def active_blocks(self, exchange: str, now: float | None = None) -> dict[str, float]:
        now = now or time.time()
        return {c: u for c, u in self.blocked.get(exchange.lower(), {}).items() if u > now}

    def total_booked(self) -> tuple[float, int]:
        rows = self.exchanges.values()
        return sum(r.booked_pnl for r in rows), sum(r.booked_trades for r in rows)

    def total_honest(self) -> float:
        return sum(r.honest_pnl for r in self.exchanges.values())


def _trade_record(row: dict) -> TradeRecord:
    path = " → ".join(f"{leg['side'][0].upper()} {leg['market']}" for leg in row.get("legs") or [])
    return TradeRecord(
        ts=float(row["ts"]),
        exchange=str(row["exchange"]),
        start_asset=str(row.get("start_asset", "")),
        start_amount=float(row.get("start_amount", 0.0)),
        end_amount=float(row.get("end_amount", 0.0)),
        net_profit=float(row.get("net_profit", 0.0)),
        net_profit_bps=float(row.get("net_profit_bps", 0.0)),
        path=path,
    )


def is_book_mismatch(detail: str) -> bool:
    """A REST mismatch about the cycle's books (not REST being down/unready)."""
    return not detail.startswith(("rest not ready", "rest error"))


def load_history(
    path: str,
    *,
    miss_limit: int,
    block_hours: float,
    block_max_hours: float = 0.0,
    since_ts: float | None = None,
    keep_recent: int = 50,
) -> tuple[PnlHistory, list[TradeRecord]]:
    """Replay booked/slipped/failed/REST-mismatch rows. Returns history and recent trades (oldest first)."""
    history = PnlHistory()
    recent: deque[TradeRecord] = deque(maxlen=keep_recent)
    file = Path(path)
    if not file.exists():
        return history, []
    with file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not any(tag in line for tag in _WANTED):
                continue
            try:
                row = json.loads(line)
                ts = float(row["ts"])
                exchange = str(row["exchange"]).lower()
                cycle_id = str(row.get("cycle", ""))
                status = row["status"]
            except (ValueError, KeyError, TypeError):
                continue
            if since_ts is not None and ts < since_ts:
                continue
            if status == "accepted":
                history.record_booked(exchange, cycle_id, float(row.get("net_profit", 0.0)), ts)
                recent.append(_trade_record(row))
                continue
            if status == "rejected":
                if row.get("reason") != "rest_mismatch" or not is_book_mismatch(str(row.get("detail", ""))):
                    continue
                status = "rest_mismatch"
            profit = row.get("realised_profit")
            streak = history.record_miss(
                exchange, cycle_id, status, float(profit) if profit is not None else None, ts
            )
            if miss_limit > 0 and streak >= miss_limit:
                history.block(exchange, cycle_id, ts, block_hours, block_max_hours)
    return history, list(recent)
