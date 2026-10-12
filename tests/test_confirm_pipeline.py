import asyncio
import time

from triangular_arbitrage.books import BookStore
from triangular_arbitrage.config import Settings
from triangular_arbitrage.execution import PaperExecutor
from triangular_arbitrage.fees import resolve_fees
from triangular_arbitrage.inventory import Inventory
from triangular_arbitrage.models import Cycle, CycleLeg, Market, Opportunity, RejectReason
from triangular_arbitrage.validator import check_sync, validate_cycle


def _leg(market: str, frm: str, to: str, side: str) -> CycleLeg:
    return CycleLeg(market, market.replace("/", ""), frm, to, side, 0.0)  # type: ignore[arg-type]


CYCLE = Cycle(
    id="tri",
    start_asset="USDT",
    legs=(
        _leg("BTC/USDT", "USDT", "BTC", "buy"),
        _leg("ETH/BTC", "BTC", "ETH", "buy"),
        _leg("ETH/USDT", "ETH", "USDT", "sell"),
    ),
)


def _books(eth_bid: float = 6.0) -> BookStore:
    books = BookStore()
    books.apply_snapshot("BTC/USDT", [(99.0, 100)], [(100.0, 100)])
    books.apply_snapshot("ETH/BTC", [(0.049, 100)], [(0.05, 100)])
    books.apply_snapshot("ETH/USDT", [(eth_bid, 100)], [(eth_bid + 0.1, 100)])
    return books


def _validate(books: BookStore) -> Opportunity:
    result = validate_cycle(
        CYCLE,
        books,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=1.0,
        stale_after=60,
    )
    assert isinstance(result, Opportunity)
    return result


def _executor(books: BookStore, latency_ms: float = 0.0, hold: float = 5.0) -> PaperExecutor:
    return PaperExecutor(
        books,
        leg_latency_ms=latency_ms,
        usage_frac=1.0,
        stale_after=60,
        consumed_hold_seconds=hold,
        min_net_profit_bps=10,
    )


# --- books: versions, crossed, consumed overlay ---


def test_out_of_order_version_is_dropped():
    books = BookStore()
    assert books.apply_snapshot("X/USDT", [(1.0, 1)], [(1.1, 1)], version=10)
    assert not books.apply_snapshot("X/USDT", [(9.0, 1)], [(9.1, 1)], version=9)
    assert books.get("X/USDT").best_bid() == 1.0


def test_consumed_liquidity_survives_snapshot_then_expires():
    books = BookStore()
    books.apply_snapshot("X/USDT", [(1.0, 10)], [(1.1, 10)])
    books.consume("X/USDT", "asks", [(1.1, 4.0)], hold_seconds=0.05)
    books.apply_snapshot("X/USDT", [(1.0, 10)], [(1.1, 10)])
    assert books.get("X/USDT").effective_asks() == [(1.1, 6.0)]
    time.sleep(0.08)
    assert books.get("X/USDT").effective_asks() == [(1.1, 10.0)]


def test_fully_consumed_level_moves_best_price():
    books = BookStore()
    books.apply_snapshot("X/USDT", [(1.0, 10)], [(1.1, 5), (1.2, 5)])
    books.consume("X/USDT", "asks", [(1.1, 5.0)], hold_seconds=5)
    assert books.get("X/USDT").best_ask() == 1.2


# --- sync check ---


def test_check_sync_rejects_crossed_book():
    books = _books()
    books.apply_snapshot("ETH/BTC", [(0.06, 10)], [(0.05, 10)])
    sync = check_sync(CYCLE, books, stale_after=60, max_feed_lag_s=3, max_leg_skew_s=2)
    assert not sync.ok and sync.reason == RejectReason.OUT_OF_SYNC


def test_check_sync_rejects_lagging_feed():
    books = _books()
    books.apply_snapshot("ETH/USDT", [(6.0, 10)], [(6.1, 10)], exchange_ts=time.time() - 5)
    sync = check_sync(CYCLE, books, stale_after=60, max_feed_lag_s=3, max_leg_skew_s=2)
    assert not sync.ok and "lag" in sync.detail


def test_check_sync_flags_leg_skew_as_suspicious():
    books = _books()
    now = time.time()
    books.apply_snapshot("BTC/USDT", [(99.0, 100)], [(100.0, 100)], exchange_ts=now - 2.5)
    books.apply_snapshot("ETH/USDT", [(6.0, 100)], [(6.1, 100)], exchange_ts=now)
    sync = check_sync(CYCLE, books, stale_after=60, max_feed_lag_s=10, max_leg_skew_s=2)
    assert sync.ok and sync.suspicious


# --- delayed paper execution ---


def test_executor_holds_and_removes_liquidity():
    books = _books()
    opp = _validate(books)
    inv = Inventory({"USDT": 1000.0})
    result = asyncio.run(_executor(books).execute(opp, inv))
    assert result.status == "held"
    assert result.realised.net_profit_bps >= 10
    assert inv.get("USDT") > 1000.0
    taken = sum(q for _p, q in result.realised.hops[0].taken)
    assert abs(books.get("BTC/USDT").effective_asks()[0][1] - (100 - taken)) < 1e-9


def test_executor_slipped_is_not_applied_to_inventory():
    books = _books()
    opp = _validate(books)
    books.apply_snapshot("ETH/USDT", [(5.0, 100)], [(5.1, 100)])
    inv = Inventory({"USDT": 1000.0})
    result = asyncio.run(_executor(books).execute(opp, inv))
    assert result.status == "slipped"
    assert inv.get("USDT") == 1000.0


def test_executor_sees_book_move_during_leg_delay():
    books = _books()
    opp = _validate(books)
    inv = Inventory({"USDT": 1000.0})

    async def run():
        task = asyncio.create_task(_executor(books, latency_ms=50).execute(opp, inv))
        await asyncio.sleep(0.01)
        books.apply_snapshot("ETH/USDT", [(5.0, 100)], [(5.1, 100)])
        return await task

    result = asyncio.run(run())
    assert result.status == "slipped"
    assert inv.get("USDT") == 1000.0


def test_executor_fails_when_book_disappears():
    books = _books()
    opp = _validate(books)
    books.invalidate("ETH/BTC")
    result = asyncio.run(_executor(books).execute(opp, Inventory({"USDT": 1000.0})))
    assert result.status == "failed"


def test_second_fill_on_same_gap_is_smaller_or_gone():
    books = BookStore()
    books.apply_snapshot("BTC/USDT", [(99.0, 1)], [(100.0, 0.3)])
    books.apply_snapshot("ETH/BTC", [(0.049, 100)], [(0.05, 100)])
    books.apply_snapshot("ETH/USDT", [(6.0, 100)], [(6.1, 100)])
    first = _validate(books)
    asyncio.run(_executor(books).execute(first, Inventory({"USDT": 1000.0})))
    again = validate_cycle(
        CYCLE,
        books,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=1.0,
        stale_after=60,
    )
    assert not isinstance(again, Opportunity) or again.start_amount < first.start_amount


def test_walk_leg_vwap_is_quote_per_base_on_both_sides():
    from triangular_arbitrage.validator import walk_leg

    books = BookStore()
    books.apply_snapshot("SUI/USDT", [(1.24, 10)], [(1.2425, 10), (1.25, 100)])
    buy = walk_leg(_leg("SUI/USDT", "USDT", "SUI", "buy"), books.get("SUI/USDT"), 20.0, 1.0)
    expected = 20.0 / (10 + (20.0 - 12.425) / 1.25)
    assert abs(buy.vwap - expected) < 1e-9
    assert 1.2425 < buy.vwap < 1.25

    sell = walk_leg(_leg("SUI/USDT", "SUI", "USDT", "sell"), books.get("SUI/USDT"), 5.0, 1.0)
    assert abs(sell.vwap - 1.24) < 1e-9


def test_per_exchange_floor_override():
    settings = Settings(min_net_profit_bps=25, min_net_profit_bps_by_exchange="mexc:60, KuCoin:40")
    assert settings.min_bps_for("mexc") == 60
    assert settings.min_bps_for("kucoin") == 40
    assert settings.min_bps_for("bingx") == 25


# --- fees ---


def test_pair_fee_override_parsing_and_precedence():
    settings = Settings(
        taker_fee_override="",
        taker_fee_pair_override="mexc:SUI/USDC=0,kucoin:tel/usdc=0.001",
        exchange_api_keys="",
    )
    assert settings.pair_fee_overrides == {"mexc": {"SUI/USDC": 0.0}, "kucoin": {"TEL/USDC": 0.001}}
    markets = [
        Market("SUI/USDC", "SUIUSDC", "SUI", "USDC", 0.0005),
        Market("SUI/USDT", "SUIUSDT", "SUI", "USDT", 0.0005),
    ]
    out, summary = asyncio.run(resolve_fees("mexc", markets, settings))
    fees = {m.unified: m.taker_fee for m in out}
    assert fees == {"SUI/USDC": 0.0, "SUI/USDT": 0.0005}
    assert "zero_fee=1" in summary
