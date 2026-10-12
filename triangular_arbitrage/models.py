from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class RejectReason(StrEnum):
    FEES = "fees"
    THIN_BOOK = "thin_book"
    SLIPPAGE = "slippage"
    BELOW_MIN_PROFIT = "below_min_profit"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RISK_LIMIT = "risk_limit"
    STALE_BOOK = "stale_book"
    PAUSED = "paused"
    KILLED = "killed"
    UNDERSIZED = "undersized"
    OUT_OF_SYNC = "out_of_sync"
    FADED = "faded"
    REST_MISMATCH = "rest_mismatch"


Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class Market:
    unified: str
    native_id: str
    base: str
    quote: str
    taker_fee: float
    min_cost: float = 0.0
    quote_volume: float = 0.0


@dataclass(frozen=True, slots=True)
class CycleLeg:
    market: str
    native_id: str
    from_asset: str
    to_asset: str
    side: Side
    taker_fee: float


@dataclass(frozen=True, slots=True)
class Cycle:
    id: str
    start_asset: str
    legs: tuple[CycleLeg, ...]

    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(leg.market for leg in self.legs)


@dataclass(frozen=True, slots=True)
class HopFill:
    market: str
    side: Side
    from_asset: str
    to_asset: str
    from_amount: float
    to_amount: float
    vwap: float
    # (price, base qty) removed from each level; drives the consumed-liquidity overlay
    taken: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class Opportunity:
    cycle: Cycle
    start_amount: float
    end_amount: float
    net_profit: float
    net_profit_bps: float
    hops: tuple[HopFill, ...]
    theoretical_product: float


@dataclass(frozen=True, slots=True)
class RejectedOpportunity:
    cycle: Cycle
    reason: RejectReason
    theoretical_product: float
    detail: str = ""


ExecStatus = Literal["held", "slipped", "failed"]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of a delayed paper fill. Only `held` results are booked as trades."""

    status: ExecStatus
    expected: Opportunity
    realised: Opportunity | None
    detail: str = ""


@dataclass
class WorkerSnapshot:
    exchange: str
    subscribed: int = 0
    books_ready: int = 0
    cycles: int = 0
    last_book_ts: float = 0.0
    last_accept: str | None = None
    last_reject: str | None = None
    paper_pnl: float = 0.0
    errors: int = 0
    paused: bool = False
    extra: dict = field(default_factory=dict)
    # candidates / faded / out_of_sync / rest_mismatch / confirmed / held / slipped / failed
    counters: dict[str, int] = field(default_factory=dict)
    fees: str = ""
    min_bps: float = 0.0
    # Held profit + slipped realised losses: what a live account would actually show
    honest_pnl: float = 0.0
    slipped_pnl: float = 0.0
    # cycle id -> blocked until (auto-blocked after repeated slips/fails)
    blocked_cycles: dict[str, float] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n
