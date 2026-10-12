from __future__ import annotations

from dataclasses import dataclass

from triangular_arbitrage.books import BookStore, LocalOrderBook
from triangular_arbitrage.models import (
    Cycle,
    CycleLeg,
    HopFill,
    Opportunity,
    RejectReason,
    RejectedOpportunity,
)


def _usable(book: LocalOrderBook | None, stale_after: float) -> bool:
    return book is not None and book.ready and book.age_seconds() <= stale_after


def top_of_book_product(cycle: Cycle, books: BookStore, stale_after: float) -> float | None:
    product = 1.0
    for leg in cycle.legs:
        book = books.get(leg.market)
        if not _usable(book, stale_after):
            return None
        rate = _top_rate(leg, book)
        if rate is None:
            return None
        product *= rate
    return product


def _top_rate(leg: CycleLeg, book: LocalOrderBook) -> float | None:
    fee_mult = 1.0 - leg.taker_fee
    if leg.side == "buy":
        ask = book.best_ask()
        if not ask:
            return None
        return (1.0 / ask) * fee_mult
    bid = book.best_bid()
    if not bid:
        return None
    return bid * fee_mult


def walk_leg(
    leg: CycleLeg,
    book: LocalOrderBook,
    from_amount: float,
    usage_frac: float,
) -> HopFill | None:
    """Fill `from_amount` against the book (net of consumed liquidity). None if too thin."""
    if from_amount <= 0:
        return None
    remaining = from_amount
    got = 0.0
    notional = 0.0
    base_filled = 0.0
    taken: list[tuple[float, float]] = []
    levels = book.effective_asks() if leg.side == "buy" else book.effective_bids()
    if not levels:
        return None
    for price, qty in levels:
        available = qty * usage_frac
        if available <= 0:
            continue
        if leg.side == "buy":
            level_cost = price * available
            take_from = min(remaining, level_cost)
            take_to = take_from / price
            base_qty = take_to
        else:
            take_from = min(remaining, available)
            take_to = take_from * price
            base_qty = take_from
        if take_from <= 0:
            continue
        remaining -= take_from
        got += take_to
        notional += take_from if leg.side == "buy" else take_to
        base_filled += base_qty
        taken.append((price, base_qty))
        if remaining <= 1e-12:
            break
    if remaining > 1e-8:
        return None
    got *= 1.0 - leg.taker_fee
    spent = from_amount
    # quote per base on both sides: buy = quote spent / base received, sell = quote received / base sold
    vwap = notional / base_filled if base_filled else 0.0
    return HopFill(
        market=leg.market,
        side=leg.side,
        from_asset=leg.from_asset,
        to_asset=leg.to_asset,
        from_amount=spent,
        to_amount=got,
        vwap=vwap,
        taken=tuple(taken),
    )


def simulate_size(
    cycle: Cycle,
    books: BookStore,
    start_amount: float,
    usage_frac: float,
    stale_after: float,
) -> tuple[tuple[HopFill, ...], float] | None:
    amount = start_amount
    hops: list[HopFill] = []
    for leg in cycle.legs:
        book = books.get(leg.market)
        if not _usable(book, stale_after):
            return None
        hop = walk_leg(leg, book, amount, usage_frac)
        if hop is None:
            return None
        hops.append(hop)
        amount = hop.to_amount
    return tuple(hops), amount


@dataclass(frozen=True, slots=True)
class SyncCheck:
    ok: bool
    # ok but legs are far apart in time: verify against REST before trading
    suspicious: bool = False
    reason: RejectReason | None = None
    detail: str = ""
    leg_skew_s: float = 0.0


def check_sync(
    cycle: Cycle,
    books: BookStore,
    *,
    stale_after: float,
    max_feed_lag_s: float,
    max_leg_skew_s: float,
) -> SyncCheck:
    """
    Book age alone proves a book updated recently, not that it is right. Reject crossed
    books and feeds lagging the exchange clock; flag legs from different moments in time.
    """
    stamps: list[float] = []
    for leg in cycle.legs:
        book = books.get(leg.market)
        if not _usable(book, stale_after):
            return SyncCheck(False, reason=RejectReason.STALE_BOOK, detail=f"{leg.market} missing/stale")
        if book.is_crossed():
            return SyncCheck(False, reason=RejectReason.OUT_OF_SYNC, detail=f"{leg.market} crossed book")
        lag = book.feed_lag_seconds()
        if max_feed_lag_s > 0 and lag is not None and lag > max_feed_lag_s:
            return SyncCheck(
                False,
                reason=RejectReason.OUT_OF_SYNC,
                detail=f"{leg.market} feed lag {lag * 1000:.0f}ms",
            )
        stamps.append(book.exchange_ts if book.exchange_ts is not None else book.ts)
    skew = max(stamps) - min(stamps) if stamps else 0.0
    suspicious = max_leg_skew_s > 0 and skew > max_leg_skew_s
    detail = f"leg skew {skew * 1000:.0f}ms" if suspicious else ""
    return SyncCheck(True, suspicious=suspicious, detail=detail, leg_skew_s=skew)


def validate_cycle(
    cycle: Cycle,
    books: BookStore,
    *,
    start_balance: float,
    min_notional: float,
    max_notional: float,
    min_net_profit_bps: float,
    usage_frac: float,
    stale_after: float,
    min_fill_notional_frac: float = 0.0,
) -> Opportunity | RejectedOpportunity:
    theoretical = top_of_book_product(cycle, books, stale_after)
    if theoretical is None:
        return RejectedOpportunity(cycle, RejectReason.STALE_BOOK, 0.0, "missing or stale book")
    theoretical_bps = (theoretical - 1.0) * 10_000
    if theoretical_bps < min_net_profit_bps:
        return RejectedOpportunity(
            cycle,
            RejectReason.FEES if theoretical <= 1.0 else RejectReason.BELOW_MIN_PROFIT,
            theoretical,
            f"top-of-book {theoretical_bps:.2f} bps",
        )

    cap = min(max_notional, start_balance)
    min_size = max(min_notional, max_notional * max(min_fill_notional_frac, 0.0))
    if cap < min_notional:
        return RejectedOpportunity(
            cycle,
            RejectReason.INSUFFICIENT_BALANCE,
            theoretical,
            f"balance {start_balance}",
        )
    if cap < min_size:
        return RejectedOpportunity(
            cycle,
            RejectReason.UNDERSIZED,
            theoretical,
            f"cap {cap:.4f} < min fill {min_size:.4f}",
        )

    lo = min_size
    hi = cap
    best: tuple[tuple[HopFill, ...], float, float] | None = None
    for _ in range(18):
        mid = (lo + hi) / 2.0
        sim = simulate_size(cycle, books, mid, usage_frac, stale_after)
        if sim is None:
            hi = mid
            continue
        hops, end_amount = sim
        profit_bps = (end_amount / mid - 1.0) * 10_000 if mid else -1e9
        if profit_bps >= min_net_profit_bps:
            best = (hops, mid, end_amount)
            lo = mid
        else:
            hi = mid

    if best is None:
        reason = RejectReason.THIN_BOOK
        if theoretical_bps >= min_net_profit_bps:
            reason = RejectReason.SLIPPAGE
        return RejectedOpportunity(
            cycle,
            reason,
            theoretical,
            "depth walk failed to keep net profit at sizeable clip",
        )

    hops, start_amount, end_amount = best
    if start_amount + 1e-9 < min_size:
        return RejectedOpportunity(
            cycle,
            RejectReason.UNDERSIZED,
            theoretical,
            f"size {start_amount:.4f} < min fill {min_size:.4f}",
        )

    profit = end_amount - start_amount
    profit_bps = (end_amount / start_amount - 1.0) * 10_000
    return Opportunity(
        cycle=cycle,
        start_amount=start_amount,
        end_amount=end_amount,
        net_profit=profit,
        net_profit_bps=profit_bps,
        hops=hops,
        theoretical_product=theoretical,
    )
