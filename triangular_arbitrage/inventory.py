from __future__ import annotations

from triangular_arbitrage.models import HopFill, Opportunity


class Inventory:
    def __init__(self, balances: dict[str, float]) -> None:
        self.balances = {asset.upper(): float(amount) for asset, amount in balances.items()}

    def get(self, asset: str) -> float:
        return self.balances.get(asset.upper(), 0.0)

    def apply(self, opportunity: Opportunity) -> None:
        self.apply_hops(opportunity.hops)

    def apply_hops(self, hops: tuple[HopFill, ...]) -> None:
        for hop in hops:
            self.balances[hop.from_asset] = self.get(hop.from_asset) - hop.from_amount
            self.balances[hop.to_asset] = self.get(hop.to_asset) + hop.to_amount

    def snapshot(self) -> dict[str, float]:
        return {asset: amount for asset, amount in sorted(self.balances.items()) if abs(amount) > 1e-12}
