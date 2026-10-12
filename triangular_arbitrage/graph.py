from __future__ import annotations

from collections import defaultdict

from triangular_arbitrage.models import Cycle, CycleLeg, Market

DEFAULT_STABLES = frozenset(
    {
        "USDT",
        "USDC",
        "USD",
        "FDUSD",
        "DAI",
        "TUSD",
        "USDE",
        "BUSD",
        "USDD",
        "USD1",
        "USDP",
        "GUSD",
        "PYUSD",
    }
)
DEFAULT_MAJORS = frozenset({"BTC", "ETH"})


def _fingerprint(legs: list[CycleLeg]) -> str:
    seq = tuple((leg.market, leg.side) for leg in legs)
    rotations = [seq[i:] + seq[:i] for i in range(len(seq))]
    best = min(rotations)
    return "|".join(f"{market}:{side}" for market, side in best)


def _legs_from_markets(markets: list[Market]) -> dict[tuple[str, str], CycleLeg]:
    edges: dict[tuple[str, str], CycleLeg] = {}
    for market in markets:
        edges[(market.quote, market.base)] = CycleLeg(
            market=market.unified,
            native_id=market.native_id,
            from_asset=market.quote,
            to_asset=market.base,
            side="buy",
            taker_fee=market.taker_fee,
        )
        edges[(market.base, market.quote)] = CycleLeg(
            market=market.unified,
            native_id=market.native_id,
            from_asset=market.base,
            to_asset=market.quote,
            side="sell",
            taker_fee=market.taker_fee,
        )
    return edges


def enumerate_cycles(
    markets: list[Market],
    start_assets: list[str],
    max_length: int = 4,
    max_4cycle_neighbors: int = 80,
) -> list[Cycle]:
    if max_length < 3:
        return []
    edges = _legs_from_markets(markets)
    adj: dict[str, list[CycleLeg]] = defaultdict(list)
    for (_frm, _to), leg in edges.items():
        adj[leg.from_asset].append(leg)

    found: dict[str, Cycle] = {}

    def consider(start: str, legs: list[CycleLeg]) -> None:
        if not legs or legs[-1].to_asset != start:
            return
        key = _fingerprint(legs)
        if key not in found:
            found[key] = Cycle(id=key, start_asset=start, legs=tuple(legs))

    for start in start_assets:
        nbs = sorted({leg.to_asset for leg in adj.get(start, [])})
        for a in nbs:
            first = edges.get((start, a))
            if first is None:
                continue
            for b in nbs:
                if a == b:
                    continue
                mid = edges.get((a, b))
                back = edges.get((b, start))
                if mid is None or back is None:
                    continue
                consider(start, [first, mid, back])

        if max_length < 4:
            continue
        four_nbs = nbs[:max_4cycle_neighbors]
        for a in four_nbs:
            first = edges.get((start, a))
            if first is None:
                continue
            for mid in adj.get(a, []):
                b = mid.to_asset
                if b == start:
                    continue
                for mid2 in adj.get(b, []):
                    c = mid2.to_asset
                    if c in {start, a, b}:
                        continue
                    back = edges.get((c, start))
                    if back is None:
                        continue
                    consider(start, [first, mid, mid2, back])
    return list(found.values())


def cycle_assets(cycle: Cycle) -> set[str]:
    assets = {cycle.start_asset}
    for leg in cycle.legs:
        assets.add(leg.from_asset)
        assets.add(leg.to_asset)
    return assets


def cycle_liquidity(cycle: Cycle, volume_by_market: dict[str, float]) -> float | None:
    """Bottleneck 24h quote volume across legs. None if any leg has unknown volume."""
    vols: list[float] = []
    for market in cycle.markets:
        vol = float(volume_by_market.get(market, 0.0) or 0.0)
        if vol <= 0:
            return None
        vols.append(vol)
    return min(vols) if vols else None


def is_low_quality_cycle(
    cycle: Cycle,
    *,
    stables: frozenset[str] = DEFAULT_STABLES,
    majors: frozenset[str] = DEFAULT_MAJORS,
) -> bool:
    """
    True for cycles that rarely print durable edge:
    - only stables (USDT↔USDC↔…)
    - only stables + majors (USDT↔USDC↔ETH noise)
    - any stable→stable hop (crowded peg legs)
    """
    assets = cycle_assets(cycle)
    if assets <= stables:
        return True
    if assets <= (stables | majors):
        return True
    for leg in cycle.legs:
        if leg.from_asset in stables and leg.to_asset in stables:
            return True
    return False


def _liquidity_score(liq: float, min_vol: float, max_vol: float) -> float:
    span = max(max_vol - min_vol, 1.0)
    prefer_target = min_vol + 0.35 * span
    return abs(liq - prefer_target) + 0.15 * liq


def _extend_pack(
    selected: list[Cycle],
    symbols: set[str],
    used_ids: set[str],
    pool: list[tuple[float, Cycle, float]],
    max_symbols: int,
) -> tuple[list[Cycle], set[str], set[str]]:
    """Greedy pack: prefer low score, then fewer new symbols (reuse books)."""
    remaining = [item for item in pool if item[1].id not in used_ids]
    while remaining:
        best_i = -1
        best_key: tuple | None = None
        for i, (score, cycle, _liq) in enumerate(remaining):
            needed = set(cycle.markets)
            extra = needed - symbols
            if len(symbols) + len(extra) > max_symbols:
                continue
            # Free cycles (all books already selected) sort first via len(extra)=0
            key = (len(extra), score, cycle.id)
            if best_key is None or key < best_key:
                best_key = key
                best_i = i
        if best_i < 0:
            break
        _score, cycle, _liq = remaining.pop(best_i)
        selected.append(cycle)
        used_ids.add(cycle.id)
        symbols |= set(cycle.markets)
    return selected, symbols, used_ids


def select_cycles(
    cycles: list[Cycle],
    max_symbols: int,
    *,
    volume_by_market: dict[str, float] | None = None,
    prefer_mid_low: bool = True,
    min_quote_volume: float = 20_000.0,
    max_quote_volume: float = 25_000_000.0,
    skip_low_quality: bool = True,
    stables: frozenset[str] = DEFAULT_STABLES,
    majors: frozenset[str] = DEFAULT_MAJORS,
    min_fill_ratio: float = 0.45,
) -> tuple[list[Cycle], set[str]]:
    """
    Pack cycles into a symbol budget.

    - skip stable↔stable and stables+majors-only cycles (crowded noise)
    - greedy pack: prefer cycles that reuse symbols already in the set
    - progressive relax of the volume band if the budget is under-filled
    """
    volume_by_market = volume_by_market or {}

    def quality_ok(cycle: Cycle) -> bool:
        if not skip_low_quality:
            return True
        return not is_low_quality_cycle(cycle, stables=stables, majors=majors)

    if not prefer_mid_low or not volume_by_market:
        ordered = sorted((c for c in cycles if quality_ok(c)), key=lambda c: (len(c.legs), c.id))
        selected: list[Cycle] = []
        symbols: set[str] = set()
        for cycle in ordered:
            needed = set(cycle.markets)
            extra = needed - symbols
            if len(symbols) + len(extra) > max_symbols:
                continue
            selected.append(cycle)
            symbols |= needed
        return selected, symbols

    # Progressive bands: tight mid/low → widen max → lower min → open ceiling
    bands = [
        (min_quote_volume, max_quote_volume),
        (min_quote_volume, max_quote_volume * 3.0),
        (max(min_quote_volume * 0.25, 1_000.0), max_quote_volume * 3.0),
        (max(min_quote_volume * 0.25, 1_000.0), float("inf")),
    ]

    selected: list[Cycle] = []
    symbols: set[str] = set()
    used_ids: set[str] = set()

    for min_vol, max_vol in bands:
        pool: list[tuple[float, Cycle, float]] = []
        for cycle in cycles:
            if cycle.id in used_ids or not quality_ok(cycle):
                continue
            liq = cycle_liquidity(cycle, volume_by_market)
            if liq is None or liq < min_vol or liq > max_vol:
                continue
            pool.append((_liquidity_score(liq, min_vol, max_vol if max_vol < float("inf") else liq * 2), cycle, liq))
        selected, symbols, used_ids = _extend_pack(selected, symbols, used_ids, pool, max_symbols)
        fill = len(symbols) / max(max_symbols, 1)
        if fill >= 0.85 or (fill >= min_fill_ratio and min_vol <= min_quote_volume * 0.25):
            break
        if len(symbols) >= max_symbols:
            break

    if len(symbols) < max_symbols * min_fill_ratio:
        # Last resort: unknown-volume cycles (still skip low-quality)
        fallback = [
            c
            for c in cycles
            if c.id not in used_ids
            and quality_ok(c)
            and cycle_liquidity(c, volume_by_market) is None
        ]
        fallback_scored = [(float(len(c.legs)), c, 0.0) for c in sorted(fallback, key=lambda x: x.id)]
        selected, symbols, used_ids = _extend_pack(
            selected, symbols, used_ids, fallback_scored, max_symbols
        )

    return selected, symbols


def index_cycles(cycles: list[Cycle]) -> dict[str, list[Cycle]]:
    index: dict[str, list[Cycle]] = {}
    for cycle in cycles:
        for market in set(cycle.markets):
            index.setdefault(market, []).append(cycle)
    return index


def volume_map(markets: list[Market]) -> dict[str, float]:
    return {m.unified: float(m.quote_volume or 0.0) for m in markets}
