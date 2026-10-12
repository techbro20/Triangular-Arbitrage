from __future__ import annotations

import asyncio
import logging

import ccxt.async_support as ccxt
from ccxt.base.errors import NetworkError, RequestTimeout

from triangular_arbitrage.models import Market

log = logging.getLogger("arb.ccxt")

# Our worker names → CCXT async class ids
CCXT_IDS = {
    "mexc": "mexc",
    "kucoin": "kucoin",
    "gateio": "gate",
    "htx": "htx",
    "bingx": "bingx",
    "bybit": "bybit",
}

# MEXC exchangeInfo is a large payload; 30s often times out on slow links.
_TIMEOUT_MS = {
    "mexc": 90_000,
    "gateio": 60_000,
}


def _quote_volume(ticker: dict) -> float:
    for key in ("quoteVolume", "quote_volume"):
        val = ticker.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    # fallback: base volume * last
    try:
        base = float(ticker.get("baseVolume") or 0.0)
        last = float(ticker.get("last") or ticker.get("close") or 0.0)
        return base * last
    except (TypeError, ValueError):
        return 0.0


def _to_markets(raw_list: list[dict], taker_override: float | None) -> list[Market]:
    markets: list[Market] = []
    for market in raw_list:
        if not market.get("spot") or market.get("active") is False:
            continue
        base = market.get("base")
        quote = market.get("quote")
        native_id = market.get("id")
        if not base or not quote or not native_id:
            continue
        taker = taker_override if taker_override is not None else float(market.get("taker") or 0.001)
        limits = (market.get("limits") or {}).get("cost") or {}
        min_cost = float(limits.get("min") or 0.0)
        info = market.get("info") or {}
        # some venues embed volume on the market object
        qv = 0.0
        for key in ("quoteVolume", "quote_volume", "volume_quote"):
            if market.get(key) is not None:
                try:
                    qv = float(market[key])
                    break
                except (TypeError, ValueError):
                    pass
            if info.get(key) is not None:
                try:
                    qv = float(info[key])
                    break
                except (TypeError, ValueError):
                    pass
        markets.append(
            Market(
                unified=str(market.get("symbol") or f"{base}/{quote}"),
                native_id=str(native_id),
                base=str(base).upper(),
                quote=str(quote).upper(),
                taker_fee=taker,
                min_cost=min_cost,
                quote_volume=qv,
            )
        )
    return markets


async def _fetch_raw_markets(client) -> list[dict]:
    if hasattr(client, "fetch_spot_markets"):
        try:
            return await client.fetch_spot_markets({})
        except TypeError:
            return await client.fetch_spot_markets()
        except (RequestTimeout, NetworkError, TimeoutError, asyncio.TimeoutError):
            # Fallback: load_markets can succeed when spot-only helper times out
            log.warning("%s fetch_spot_markets timed out; falling back to load_markets", client.id)
            raw = await client.load_markets(reload=True)
            return list(raw.values())
    raw = await client.load_markets()
    return list(raw.values())


def _client(exchange_id: str, credentials: dict | None = None):
    ccxt_id = CCXT_IDS.get(exchange_id, exchange_id)
    if not hasattr(ccxt, ccxt_id):
        raise ValueError(f"CCXT has no exchange '{ccxt_id}' (from '{exchange_id}')")
    config: dict = {
        "enableRateLimit": True,
        "timeout": _TIMEOUT_MS.get(exchange_id, 45_000),
        "options": {"defaultType": "spot"},
    }
    if credentials:
        config.update({k: v for k, v in credentials.items() if v})
    return getattr(ccxt, ccxt_id)(config)


# Limits every venue accepts for fetch_order_book (KuCoin 20/100, HTX 5/10/20/150).
_REST_BOOK_LIMIT = 20


class RestBookClient:
    """REST order books for double-checking suspicious websocket gaps."""

    def __init__(self, exchange_id: str, timeout_s: float = 3.0) -> None:
        self.exchange_id = exchange_id
        self.timeout_s = timeout_s
        self._client = _client(exchange_id)
        self.ready = False

    async def warm(self) -> None:
        """fetch_order_book needs markets loaded; do it up front (MEXC can take ~60s)."""
        for attempt in range(3):
            try:
                await self._client.load_markets()
                self.ready = True
                return
            except Exception as exc:
                log.warning("%s REST verify warm-up failed (%s), attempt %s", self.exchange_id, exc, attempt + 1)
                await asyncio.sleep(5 * (attempt + 1))

    async def fetch_books(
        self,
        symbols: list[str],
        depth: int,
    ) -> dict[str, tuple[list[tuple[float, float]], list[tuple[float, float]]]]:
        async def one(symbol: str):
            book = await self._client.fetch_order_book(symbol, _REST_BOOK_LIMIT)
            bids = [(float(p), float(q)) for p, q, *_ in book.get("bids") or []][:depth]
            asks = [(float(p), float(q)) for p, q, *_ in book.get("asks") or []][:depth]
            return symbol, bids, asks

        results = await asyncio.wait_for(
            asyncio.gather(*(one(s) for s in symbols)),
            timeout=self.timeout_s,
        )
        return {symbol: (bids, asks) for symbol, bids, asks in results}

    async def close(self) -> None:
        await self._client.close()


async def fetch_account_taker_fees(exchange_id: str, credentials: dict) -> dict[str, float]:
    """Per-symbol taker fees for YOUR account tier (needs read-only API keys). {} if unavailable."""
    client = _client(exchange_id, credentials)
    try:
        if not client.has.get("fetchTradingFees"):
            log.info("%s has no fetchTradingFees in CCXT; using public fees", exchange_id)
            return {}
        fees = await client.fetch_trading_fees()
        out: dict[str, float] = {}
        for symbol, row in (fees or {}).items():
            taker = row.get("taker") if isinstance(row, dict) else None
            if taker is not None:
                out[str(symbol)] = float(taker)
        return out
    except Exception as exc:
        log.warning("%s account fee fetch failed (%s); using public fees", exchange_id, exc)
        return {}
    finally:
        await client.close()


async def load_spot_markets(exchange_id: str, taker_override: float | None = None) -> list[Market]:
    ccxt_id = CCXT_IDS.get(exchange_id, exchange_id)
    if not hasattr(ccxt, ccxt_id):
        raise ValueError(
            f"CCXT has no exchange '{ccxt_id}' (from '{exchange_id}'). "
            f"Supported: {', '.join(sorted(CCXT_IDS))}"
        )
    klass = getattr(ccxt, ccxt_id)
    timeout = _TIMEOUT_MS.get(exchange_id, 45_000)
    last_exc: BaseException | None = None

    for attempt in range(1, 4):
        client = klass(
            {
                "enableRateLimit": True,
                "timeout": timeout,
                "options": {"defaultType": "spot"},
            }
        )
        try:
            raw_list = await _fetch_raw_markets(client)
            markets = _to_markets(raw_list, taker_override)

            # Enrich with 24h quote volume for mid/low liquidity selection.
            if client.has.get("fetchTickers"):
                try:
                    tickers = await client.fetch_tickers()
                    vol_map = {
                        str(sym): _quote_volume(ticker)
                        for sym, ticker in tickers.items()
                        if isinstance(ticker, dict)
                    }
                    markets = [
                        Market(
                            unified=m.unified,
                            native_id=m.native_id,
                            base=m.base,
                            quote=m.quote,
                            taker_fee=m.taker_fee,
                            min_cost=m.min_cost,
                            quote_volume=vol_map.get(m.unified, m.quote_volume),
                        )
                        for m in markets
                    ]
                    known = sum(1 for m in markets if m.quote_volume > 0)
                    log.info(
                        "%s volume enrichment: %s/%s markets with quoteVolume",
                        exchange_id,
                        known,
                        len(markets),
                    )
                except Exception:
                    log.exception("%s fetch_tickers failed; using unranked volumes", exchange_id)
            return markets
        except (RequestTimeout, NetworkError, TimeoutError, asyncio.TimeoutError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 15)
            log.warning(
                "%s market load attempt %s/3 failed (%s); retry in %ss",
                exchange_id,
                attempt,
                type(exc).__name__,
                wait,
            )
            await asyncio.sleep(wait)
        finally:
            await client.close()

    assert last_exc is not None
    raise last_exc
