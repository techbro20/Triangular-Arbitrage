from triangular_arbitrage.models import Cycle, CycleLeg, Opportunity
from triangular_arbitrage.trades import TradeBook


def test_trade_book_records_and_lists():
    book = TradeBook(max_trades=5)
    legs = (
        CycleLeg("BTC/USDT", "BTCUSDT", "USDT", "BTC", "buy", 0.0),
        CycleLeg("ETH/BTC", "ETHBTC", "BTC", "ETH", "buy", 0.0),
        CycleLeg("ETH/USDT", "ETHUSDT", "ETH", "USDT", "sell", 0.0),
    )
    cycle = Cycle(id="x", start_asset="USDT", legs=legs)
    opp = Opportunity(
        cycle=cycle,
        start_amount=10.0,
        end_amount=10.2,
        net_profit=0.2,
        net_profit_bps=200.0,
        hops=(),
        theoretical_product=1.02,
    )
    row = book.record("mexc", opp)
    assert book.count_for("mexc") == 1
    assert book.total_count() == 1
    assert "M" in row.path or row.path == ""  # empty hops path ok
    assert book.recent(1)[0].net_profit == 0.2
    assert "+" in row.short_line() or "0.2000" in row.short_line()
