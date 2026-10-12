from __future__ import annotations

from dataclasses import dataclass, field

from triangular_arbitrage.config import Settings
from triangular_arbitrage.models import RejectReason


@dataclass
class RiskManager:
    """Session risk — PnL accumulates until process restart (never midnight-resets)."""

    settings: Settings
    killed: bool = False
    paused_global: bool = False
    paused: set[str] = field(default_factory=set)
    # Kept name for compatibility; values are session totals, not calendar-day.
    daily_pnl: dict[str, float] = field(default_factory=dict)
    # Candidates between confirm and fill; MAX_CONCURRENT applies per exchange.
    in_flight_by: dict[str, int] = field(default_factory=dict)

    @property
    def in_flight(self) -> int:
        return sum(self.in_flight_by.values())

    def acquire(self, exchange: str) -> None:
        key = exchange.lower()
        self.in_flight_by[key] = self.in_flight_by.get(key, 0) + 1

    def release(self, exchange: str) -> None:
        key = exchange.lower()
        self.in_flight_by[key] = max(0, self.in_flight_by.get(key, 0) - 1)

    def pause(self, exchange: str | None = None) -> None:
        if exchange:
            self.paused.add(exchange.lower())
        else:
            self.paused_global = True

    def resume(self, exchange: str | None = None) -> None:
        if exchange:
            self.paused.discard(exchange.lower())
        else:
            self.paused_global = False
            self.paused.clear()

    def kill(self) -> None:
        self.killed = True
        self.paused_global = True

    def is_paused(self, exchange: str) -> bool:
        return self.killed or self.paused_global or exchange.lower() in self.paused

    def record_pnl(self, exchange: str, pnl: float) -> None:
        key = exchange.lower()
        self.daily_pnl[key] = self.daily_pnl.get(key, 0.0) + pnl
        if self.daily_pnl[key] <= -abs(self.settings.daily_loss_limit):
            self.pause(key)

    def allow(self, exchange: str, notional: float) -> RejectReason | None:
        if self.killed:
            return RejectReason.KILLED
        if self.is_paused(exchange):
            return RejectReason.PAUSED
        if notional > self.settings.max_notional + 1e-9:
            return RejectReason.RISK_LIMIT
        if self.in_flight_by.get(exchange.lower(), 0) >= self.settings.max_concurrent:
            return RejectReason.RISK_LIMIT
        pnl = self.daily_pnl.get(exchange.lower(), 0.0)
        if pnl <= -abs(self.settings.daily_loss_limit):
            return RejectReason.RISK_LIMIT
        return None

    def session_pnl(self, exchange: str) -> float:
        return self.daily_pnl.get(exchange.lower(), 0.0)

    def total_session_pnl(self) -> float:
        return sum(self.daily_pnl.values())
