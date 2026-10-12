from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
import uuid

import websockets

from triangular_arbitrage.exchange.base import BookHandler, DisconnectHandler
from triangular_arbitrage.exchange.ccxt_rest import load_spot_markets
from triangular_arbitrage.exchange.common import (
    BACKOFF_MAX,
    BACKOFF_START,
    RECV_TIMEOUT,
    FeedStats,
    chunks,
    levels_from_rows,
)
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.bingx")

WS_URL = "wss://open-api-ws.bingx.com/market"


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return gzip.decompress(raw).decode("utf-8")
    except Exception:
        return raw.decode("utf-8")


class BingxAdapter:
    name = "bingx"

    def __init__(self, *, book_depth: int, taker_override: float | None, subs_per_conn: int = 20) -> None:
        depth = 5 if book_depth <= 5 else (10 if book_depth <= 10 else (20 if book_depth <= 20 else 50))
        self.depth = depth
        self.taker_override = taker_override
        self.subs_per_conn = subs_per_conn
        self._native_to_unified: dict[str, str] = {}
        self.stats = FeedStats(mode=f"depth{depth}")

    async def load_markets(self) -> list[Market]:
        return await load_spot_markets(self.name, self.taker_override)

    async def stream_books(
        self,
        native_by_unified: dict[str, str],
        on_snapshot: BookHandler,
        stop_event: asyncio.Event,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        # BingX uses BTC-USDT style
        self._native_to_unified = {n.upper(): u for u, n in native_by_unified.items()}
        groups = chunks(list(native_by_unified.values()), self.subs_per_conn)
        tasks = [
            asyncio.create_task(self._run_connection(g, on_snapshot, stop_event, on_disconnect))
            for g in groups
        ]
        try:
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_connection(self, natives, on_snapshot, stop_event, on_disconnect) -> None:
        unifieds = [self._native_to_unified[n.upper()] for n in natives if n.upper() in self._native_to_unified]
        backoff = BACKOFF_START
        while not stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                    for native in natives:
                        data_type = f"{native}@depth{self.depth}"
                        await ws.send(
                            json.dumps(
                                {
                                    "id": str(uuid.uuid4()),
                                    "reqType": "sub",
                                    "dataType": data_type,
                                }
                            )
                        )
                    while not stop_event.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                        await self._handle(raw, on_snapshot, ws)
                        backoff = BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.reconnects += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("BingX ws drop: %s", self.stats.last_error)
                if on_disconnect:
                    await on_disconnect(unifieds)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _handle(self, raw, on_snapshot, ws) -> None:
        try:
            text = _decode(raw)
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.last_error = f"decode: {exc}"
            return
        if "ping" in text.lower() and "dataType" not in text:
            try:
                await ws.send("Pong")
            except Exception:
                return
            self.stats.last_message_ts = time.time()
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.stats.parse_errors += 1
            return
        if isinstance(payload, dict) and str(payload.get("ping") or ""):
            await ws.send("Pong")
            self.stats.last_message_ts = time.time()
            return
        data_type = str(payload.get("dataType") or "")
        data = payload.get("data") or {}
        native = data_type.split("@")[0] if "@" in data_type else str(data.get("s") or data.get("symbol") or "")
        unified = self._native_to_unified.get(native.upper())
        if not unified:
            return
        bids = levels_from_rows(data.get("bids") or data.get("b"))
        asks = levels_from_rows(data.get("asks") or data.get("a"))
        if not (bids and asks):
            return
        self.stats.snapshots += 1
        self.stats.last_message_ts = time.time()
        await on_snapshot(unified, bids, asks)
