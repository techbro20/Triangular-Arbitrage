from triangular_arbitrage.books import BookStore
from triangular_arbitrage.models import Cycle, CycleLeg, RejectReason
from triangular_arbitrage.validator import top_of_book_product, validate_cycle


def _leg(market: str, frm: str, to: str, side: str, fee: float = 0.001) -> CycleLeg:
    return CycleLeg(
        market=market,
        native_id=market.replace("/", ""),
        from_asset=frm,
        to_asset=to,
        side=side,  # type: ignore[arg-type]
        taker_fee=fee,
    )


def _triangle(fee: float = 0.0) -> Cycle:
    legs = (
        _leg("BTC/USDT", "USDT", "BTC", "buy", fee),
        _leg("ETH/BTC", "BTC", "ETH", "buy", fee),
        _leg("ETH/USDT", "ETH", "USDT", "sell", fee),
    )
    return Cycle(id="tri", start_asset="USDT", legs=legs)


def test_top_of_book_product_with_fees():
    # buy BTC @ 100 ask -> 0.01 BTC * (1-fee)
    # buy ETH @ 0.05 ask (BTC) -> ETH
    # sell ETH @ 6 bid (USDT)
    # With fee=0: (1/100)*(1/0.05)*6 = 1.2
    cycle = _triangle(fee=0.0)
    books = BookStore()
    books.apply_snapshot("BTC/USDT", bids=[(99.0, 10)], asks=[(100.0, 10)])
    books.apply_snapshot("ETH/BTC", bids=[(0.049, 10)], asks=[(0.05, 10)])
    books.apply_snapshot("ETH/USDT", bids=[(6.0, 10)], asks=[(6.1, 10)])
    product = top_of_book_product(cycle, books, stale_after=60)
    assert product is not None
    assert abs(product - 1.2) < 1e-9


def test_validate_accepts_deep_profitable_cycle():
    cycle = _triangle(fee=0.001)
    books = BookStore()
    books.apply_snapshot("BTC/USDT", bids=[(99.0, 100)], asks=[(100.0, 100)])
    books.apply_snapshot("ETH/BTC", bids=[(0.049, 100)], asks=[(0.05, 100)])
    books.apply_snapshot("ETH/USDT", bids=[(6.0, 100)], asks=[(6.1, 100)])
    result = validate_cycle(
        cycle,
        books,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=1.0,
        stale_after=60,
    )
    from triangular_arbitrage.models import Opportunity

    assert isinstance(result, Opportunity)
    assert result.net_profit_bps >= 10
    assert result.start_amount >= 5


def test_validate_rejects_when_fees_kill_edge():
    # product without fees ~1.0 exactly on flat books: 1/100 * 1/0.05 * 5 = 1.0
    cycle = _triangle(fee=0.001)
    books = BookStore()
    books.apply_snapshot("BTC/USDT", bids=[(99.0, 100)], asks=[(100.0, 100)])
    books.apply_snapshot("ETH/BTC", bids=[(0.049, 100)], asks=[(0.05, 100)])
    books.apply_snapshot("ETH/USDT", bids=[(5.0, 100)], asks=[(5.1, 100)])
    result = validate_cycle(
        cycle,
        books,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=5,
        usage_frac=1.0,
        stale_after=60,
    )
    from triangular_arbitrage.models import RejectedOpportunity

    assert isinstance(result, RejectedOpportunity)
    assert result.reason in {RejectReason.FEES, RejectReason.BELOW_MIN_PROFIT}


def test_validate_rejects_thin_book():
    cycle = _triangle(fee=0.0)
    books = BookStore()
    books.apply_snapshot("BTC/USDT", bids=[(99.0, 0.001)], asks=[(100.0, 0.001)])
    books.apply_snapshot("ETH/BTC", bids=[(0.049, 0.001)], asks=[(0.05, 0.001)])
    books.apply_snapshot("ETH/USDT", bids=[(6.0, 0.001)], asks=[(6.1, 0.001)])
    result = validate_cycle(
        cycle,
        books,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=0.25,
        stale_after=60,
    )
    from triangular_arbitrage.models import RejectedOpportunity

    assert isinstance(result, RejectedOpportunity)
    assert result.reason in {
        RejectReason.THIN_BOOK,
        RejectReason.SLIPPAGE,
        RejectReason.BELOW_MIN_PROFIT,
    }


def test_validate_rejects_insufficient_balance():
    cycle = _triangle(fee=0.0)
    books = BookStore()
    books.apply_snapshot("BTC/USDT", bids=[(99.0, 100)], asks=[(100.0, 100)])
    books.apply_snapshot("ETH/BTC", bids=[(0.049, 100)], asks=[(0.05, 100)])
    books.apply_snapshot("ETH/USDT", bids=[(6.0, 100)], asks=[(6.1, 100)])
    result = validate_cycle(
        cycle,
        books,
        start_balance=1,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=1.0,
        stale_after=60,
    )
    from triangular_arbitrage.models import RejectedOpportunity

    assert isinstance(result, RejectedOpportunity)
    assert result.reason == RejectReason.INSUFFICIENT_BALANCE


def test_validate_rejects_undersized_when_only_min_clip_works():
    # Deep enough for tiny size but force min fill = 50% of max → still ok with deep books
    cycle = _triangle(fee=0.0)
    books = BookStore()
    books.apply_snapshot("BTC/USDT", bids=[(99.0, 100)], asks=[(100.0, 100)])
    books.apply_snapshot("ETH/BTC", bids=[(0.049, 100)], asks=[(0.05, 100)])
    books.apply_snapshot("ETH/USDT", bids=[(6.0, 100)], asks=[(6.1, 100)])
    ok = validate_cycle(
        cycle,
        books,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=1.0,
        stale_after=60,
        min_fill_notional_frac=0.5,
    )
    from triangular_arbitrage.models import Opportunity

    assert isinstance(ok, Opportunity)
    assert ok.start_amount >= 25.0

    # Ultra-thin levels: TOB looks fine but cannot size to $25
    thin = BookStore()
    thin.apply_snapshot("BTC/USDT", bids=[(99.0, 0.01)], asks=[(100.0, 0.01)])
    thin.apply_snapshot("ETH/BTC", bids=[(0.049, 0.01)], asks=[(0.05, 0.01)])
    thin.apply_snapshot("ETH/USDT", bids=[(6.0, 0.01)], asks=[(6.1, 0.01)])
    result = validate_cycle(
        cycle,
        thin,
        start_balance=1000,
        min_notional=5,
        max_notional=50,
        min_net_profit_bps=10,
        usage_frac=0.25,
        stale_after=60,
        min_fill_notional_frac=0.5,
    )
    from triangular_arbitrage.models import RejectedOpportunity

    assert isinstance(result, RejectedOpportunity)
    assert result.reason in {
        RejectReason.UNDERSIZED,
        RejectReason.SLIPPAGE,
        RejectReason.THIN_BOOK,
    }