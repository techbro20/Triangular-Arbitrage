from triangular_arbitrage.graph import (
    cycle_liquidity,
    enumerate_cycles,
    index_cycles,
    is_low_quality_cycle,
    select_cycles,
)
from triangular_arbitrage.models import Market


def _m(unified: str, base: str, quote: str, fee: float = 0.001, vol: float = 0.0) -> Market:
    return Market(
        unified=unified,
        native_id=unified.replace("/", ""),
        base=base,
        quote=quote,
        taker_fee=fee,
        quote_volume=vol,
    )


def test_enumerate_finds_triangle():
    markets = [
        _m("BTC/USDT", "BTC", "USDT"),
        _m("ETH/USDT", "ETH", "USDT"),
        _m("ETH/BTC", "ETH", "BTC"),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    assert len(cycles) >= 1
    assert all(len(c.legs) == 3 for c in cycles)
    assert all(c.start_asset == "USDT" for c in cycles)


def test_enumerate_empty_without_start():
    markets = [_m("BTC/USDT", "BTC", "USDT")]
    assert enumerate_cycles(markets, ["EUR"], max_length=3) == []


def test_select_cycles_respects_symbol_budget():
    markets = [
        _m("BTC/USDT", "BTC", "USDT", vol=1e9),
        _m("ETH/USDT", "ETH", "USDT", vol=1e9),
        _m("ETH/BTC", "ETH", "BTC", vol=1e9),
        _m("SOL/USDT", "SOL", "USDT", vol=5e5),
        _m("SOL/BTC", "SOL", "BTC", vol=5e5),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    selected, symbols = select_cycles(
        cycles,
        max_symbols=3,
        volume_by_market={m.unified: m.quote_volume for m in markets},
        prefer_mid_low=True,
        min_quote_volume=1_000,
        max_quote_volume=1e8,
    )
    assert len(symbols) <= 3
    assert all(set(c.markets).issubset(symbols) for c in selected)


def test_select_cycles_prefers_mid_low_over_majors():
    # Major triangle: very high volume
    # Alt triangle: mid volume
    markets = [
        _m("BTC/USDT", "BTC", "USDT", vol=2e9),
        _m("ETH/USDT", "ETH", "USDT", vol=1e9),
        _m("ETH/BTC", "ETH", "BTC", vol=5e8),
        _m("AAA/USDT", "AAA", "USDT", vol=200_000),
        _m("BBB/USDT", "BBB", "USDT", vol=180_000),
        _m("AAA/BBB", "AAA", "BBB", vol=150_000),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    vol = {m.unified: m.quote_volume for m in markets}
    selected, symbols = select_cycles(
        cycles,
        max_symbols=3,
        volume_by_market=vol,
        prefer_mid_low=True,
        min_quote_volume=20_000,
        max_quote_volume=25_000_000,
    )
    assert symbols == {"AAA/USDT", "BBB/USDT", "AAA/BBB"}
    assert all("BTC" not in s and "ETH" not in s for s in symbols)


def test_select_cycles_skips_dust():
    markets = [
        _m("DUST/USDT", "DUST", "USDT", vol=100),
        _m("AAA/USDT", "AAA", "USDT", vol=100),
        _m("DUST/AAA", "DUST", "AAA", vol=50),
        _m("BBB/USDT", "BBB", "USDT", vol=200_000),
        _m("CCC/USDT", "CCC", "USDT", vol=180_000),
        _m("BBB/CCC", "BBB", "CCC", vol=150_000),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    vol = {m.unified: m.quote_volume for m in markets}
    selected, symbols = select_cycles(
        cycles,
        max_symbols=10,
        volume_by_market=vol,
        prefer_mid_low=True,
        min_quote_volume=20_000,
        max_quote_volume=25_000_000,
    )
    assert "DUST/USDT" not in symbols
    assert "BBB/USDT" in symbols


def test_cycle_liquidity_bottleneck():
    markets = [
        _m("A/USDT", "A", "USDT", vol=100),
        _m("B/USDT", "B", "USDT", vol=50),
        _m("A/B", "A", "B", vol=200),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    vol = {m.unified: m.quote_volume for m in markets}
    assert cycles
    assert cycle_liquidity(cycles[0], vol) == 50.0


def test_index_cycles_maps_markets():
    markets = [
        _m("BTC/USDT", "BTC", "USDT"),
        _m("ETH/USDT", "ETH", "USDT"),
        _m("ETH/BTC", "ETH", "BTC"),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    index = index_cycles(cycles)
    assert "BTC/USDT" in index
    assert index["BTC/USDT"]


def test_skips_stable_major_noise():
    # USDT→USDC→ETH junk + a real alt triangle
    markets = [
        _m("USDC/USDT", "USDC", "USDT", vol=5e7),
        _m("ETH/USDT", "ETH", "USDT", vol=5e6),
        _m("ETH/USDC", "ETH", "USDC", vol=4e6),
        _m("AAA/USDT", "AAA", "USDT", vol=200_000),
        _m("BBB/USDT", "BBB", "USDT", vol=180_000),
        _m("AAA/BBB", "AAA", "BBB", vol=150_000),
    ]
    cycles = enumerate_cycles(markets, ["USDT", "USDC"], max_length=3)
    junk = [c for c in cycles if is_low_quality_cycle(c)]
    assert junk, "expected stables+majors cycles to be flagged"
    vol = {m.unified: m.quote_volume for m in markets}
    selected, symbols = select_cycles(
        cycles,
        max_symbols=10,
        volume_by_market=vol,
        prefer_mid_low=True,
        min_quote_volume=20_000,
        max_quote_volume=25_000_000,
        skip_low_quality=True,
    )
    assert all(not is_low_quality_cycle(c) for c in selected)
    assert "AAA/USDT" in symbols
    assert "USDC/USDT" not in symbols


def test_greedy_pack_reuses_symbols():
    # Two alt triangles sharing AAA/USDT — budget of 5 should fit both if packing reuses
    markets = [
        _m("AAA/USDT", "AAA", "USDT", vol=200_000),
        _m("BBB/USDT", "BBB", "USDT", vol=190_000),
        _m("AAA/BBB", "AAA", "BBB", vol=180_000),
        _m("CCC/USDT", "CCC", "USDT", vol=170_000),
        _m("AAA/CCC", "AAA", "CCC", vol=160_000),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    vol = {m.unified: m.quote_volume for m in markets}
    selected, symbols = select_cycles(
        cycles,
        max_symbols=5,
        volume_by_market=vol,
        prefer_mid_low=True,
        min_quote_volume=20_000,
        max_quote_volume=25_000_000,
        skip_low_quality=True,
    )
    assert len(symbols) <= 5
    assert len(selected) >= 2


def test_progressive_relax_fills_when_band_empty():
    # Only high-volume alts — tight max_quote_volume would exclude them; relax should recover
    markets = [
        _m("AAA/USDT", "AAA", "USDT", vol=80_000_000),
        _m("BBB/USDT", "BBB", "USDT", vol=70_000_000),
        _m("AAA/BBB", "AAA", "BBB", vol=60_000_000),
    ]
    cycles = enumerate_cycles(markets, ["USDT"], max_length=3)
    vol = {m.unified: m.quote_volume for m in markets}
    selected, symbols = select_cycles(
        cycles,
        max_symbols=10,
        volume_by_market=vol,
        prefer_mid_low=True,
        min_quote_volume=20_000,
        max_quote_volume=25_000_000,
        skip_low_quality=True,
        min_fill_ratio=0.3,
    )
    assert selected
    assert "AAA/USDT" in symbols
