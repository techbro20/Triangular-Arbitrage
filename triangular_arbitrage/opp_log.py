from __future__ import annotations

import json
import os
import time
from pathlib import Path

from triangular_arbitrage.models import ExecutionResult, Opportunity, RejectedOpportunity


class OpportunityLog:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_accepted(self, exchange: str, opportunity: Opportunity) -> None:
        self._write(
            {
                "ts": time.time(),
                "exchange": exchange,
                "status": "accepted",
                "cycle": opportunity.cycle.id,
                "start_asset": opportunity.cycle.start_asset,
                "start_amount": opportunity.start_amount,
                "end_amount": opportunity.end_amount,
                "net_profit": opportunity.net_profit,
                "net_profit_bps": opportunity.net_profit_bps,
                "theoretical_product": opportunity.theoretical_product,
                "legs": [
                    {
                        "market": hop.market,
                        "side": hop.side,
                        "from": hop.from_asset,
                        "to": hop.to_asset,
                        "from_amount": hop.from_amount,
                        "to_amount": hop.to_amount,
                        "vwap": hop.vwap,
                    }
                    for hop in opportunity.hops
                ],
            }
        )

    def write_execution_miss(self, exchange: str, result: ExecutionResult) -> None:
        """Slipped/failed delayed fills — tracked here only, never booked as trades."""
        realised = result.realised
        self._write(
            {
                "ts": time.time(),
                "exchange": exchange,
                "status": result.status,
                "cycle": result.expected.cycle.id,
                "start_asset": result.expected.cycle.start_asset,
                "expected_bps": result.expected.net_profit_bps,
                "realised_bps": realised.net_profit_bps if realised else None,
                "start_amount": realised.start_amount if realised else result.expected.start_amount,
                "realised_profit": realised.net_profit if realised else None,
                "detail": result.detail,
            }
        )

    def write_rejected(self, exchange: str, rejected: RejectedOpportunity) -> None:
        self._write(
            {
                "ts": time.time(),
                "exchange": exchange,
                "status": "rejected",
                "cycle": rejected.cycle.id,
                "reason": rejected.reason.value,
                "detail": rejected.detail,
                "theoretical_product": rejected.theoretical_product,
            }
        )

    def _write(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + os.linesep)
