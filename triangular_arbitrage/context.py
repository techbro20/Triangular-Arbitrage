from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from triangular_arbitrage.alerts import AlertHub
from triangular_arbitrage.config import Settings
from triangular_arbitrage.history import PnlHistory
from triangular_arbitrage.models import WorkerSnapshot
from triangular_arbitrage.opp_log import OpportunityLog
from triangular_arbitrage.risk import RiskManager
from triangular_arbitrage.trades import TradeBook


@dataclass
class AppContext:
    settings: Settings
    risk: RiskManager
    alerts: AlertHub
    opp_log: OpportunityLog
    stop: asyncio.Event
    statuses: dict[str, WorkerSnapshot] = field(default_factory=dict)
    trades: TradeBook = field(default_factory=TradeBook)
    history: PnlHistory = field(default_factory=PnlHistory)
