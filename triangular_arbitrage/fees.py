from __future__ import annotations

import logging
from dataclasses import replace

from triangular_arbitrage.config import Settings
from triangular_arbitrage.exchange.ccxt_rest import fetch_account_taker_fees
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.fees")


async def resolve_fees(exchange: str, markets: list[Market], settings: Settings) -> tuple[list[Market], str]:
    """
    Taker fee per pair, highest priority first:
    1. TAKER_FEE_PAIR_OVERRIDE (e.g. zero-fee USDC promos)
    2. TAKER_FEE_OVERRIDE for the whole exchange (already applied when markets loaded)
    3. Account fees via read-only EXCHANGE_API_KEYS (your real tier)
    4. CCXT public default
    """
    pair_overrides = settings.pair_fee_overrides.get(exchange, {})
    exchange_override = settings.fee_overrides.get(exchange)
    creds = settings.api_keys_map().get(exchange)

    account: dict[str, float] = {}
    if exchange_override is None and creds:
        account = await fetch_account_taker_fees(exchange, creds)

    if exchange_override is not None:
        source = "exchange_override"
    elif account:
        source = "account"
    else:
        source = "public"

    out: list[Market] = []
    for market in markets:
        fee = market.taker_fee
        if exchange_override is None and market.unified in account:
            fee = account[market.unified]
        if market.unified in pair_overrides:
            fee = pair_overrides[market.unified]
        out.append(market if fee == market.taker_fee else replace(market, taker_fee=fee))

    fees = [m.taker_fee for m in out]
    zero = sum(1 for f in fees if f <= 0)
    summary = (
        f"source={source} pairs={len(fees)} "
        f"range={min(fees) * 100:.3f}%-{max(fees) * 100:.3f}% "
        f"zero_fee={zero} pair_overrides={len(pair_overrides)}"
        if fees
        else f"source={source} pairs=0"
    )
    unknown = [p for p in pair_overrides if p not in {m.unified for m in out}]
    if unknown:
        log.warning("%s pair fee overrides for unknown symbols: %s", exchange, ", ".join(unknown))
    log.info("%s fees: %s", exchange, summary)
    return out, summary
