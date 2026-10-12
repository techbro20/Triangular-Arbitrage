from __future__ import annotations

import asyncio

from triangular_arbitrage.books import BookStore
from triangular_arbitrage.inventory import Inventory
from triangular_arbitrage.models import ExecutionResult, HopFill, Opportunity
from triangular_arbitrage.validator import walk_leg


class PaperExecutor:
    """
    Paper fills that respect time: leg 1 fills on the current book, each later leg waits
    `leg_latency_ms` and fills on whatever the book shows then. Liquidity taken by every
    leg is removed from the local book for `consumed_hold_seconds`.

    Only results that still clear `min_net_profit_bps` are `held` and applied to inventory;
    the rest come back as `slipped`/`failed` for separate tracking.
    """

    def __init__(
        self,
        books: BookStore,
        *,
        leg_latency_ms: float,
        usage_frac: float,
        stale_after: float,
        consumed_hold_seconds: float,
        min_net_profit_bps: float,
    ) -> None:
        self.books = books
        self.leg_latency_s = max(leg_latency_ms, 0.0) / 1000.0
        self.usage_frac = usage_frac
        self.stale_after = stale_after
        self.consumed_hold_seconds = consumed_hold_seconds
        self.min_net_profit_bps = min_net_profit_bps

    async def execute(
        self,
        opportunity: Opportunity,
        inventory: Inventory,
        start_amount: float | None = None,
    ) -> ExecutionResult:
        cycle = opportunity.cycle
        size = start_amount if start_amount is not None else opportunity.start_amount
        amount = size
        hops: list[HopFill] = []

        for index, leg in enumerate(cycle.legs):
            if index > 0 and self.leg_latency_s > 0:
                await asyncio.sleep(self.leg_latency_s)
            book = self.books.get(leg.market)
            if book is None or not book.ready or book.age_seconds() > self.stale_after:
                return ExecutionResult("failed", opportunity, None, f"leg {index + 1} {leg.market} book gone")
            hop = walk_leg(leg, book, amount, self.usage_frac)
            if hop is None:
                return ExecutionResult(
                    "failed",
                    opportunity,
                    None,
                    f"leg {index + 1} {leg.market} too thin after {self.leg_latency_s * 1000:.0f}ms",
                )
            self.books.consume(
                leg.market,
                "asks" if leg.side == "buy" else "bids",
                hop.taken,
                self.consumed_hold_seconds,
            )
            hops.append(hop)
            amount = hop.to_amount

        profit = amount - size
        profit_bps = (amount / size - 1.0) * 10_000 if size else -1e9
        realised = Opportunity(
            cycle=cycle,
            start_amount=size,
            end_amount=amount,
            net_profit=profit,
            net_profit_bps=profit_bps,
            hops=tuple(hops),
            theoretical_product=opportunity.theoretical_product,
        )
        if profit_bps + 1e-9 < self.min_net_profit_bps:
            return ExecutionResult(
                "slipped",
                opportunity,
                realised,
                f"expected {opportunity.net_profit_bps:.1f}bps, got {profit_bps:.1f}bps",
            )
        inventory.apply_hops(realised.hops)
        return ExecutionResult("held", opportunity, realised)


class LiveExecutor:
    async def execute(
        self,
        opportunity: Opportunity,
        inventory: Inventory,
        start_amount: float | None = None,
    ) -> ExecutionResult:
        raise RuntimeError(
            "Live trading is disabled. Keep TRADING_MODE=paper until paper results are stable, "
            "then set TRADING_MODE=live and ENABLE_LIVE=true."
        )
