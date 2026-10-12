from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time

import websockets

from triangular_arbitrage.exchange.base import BookHandler, DisconnectHandler
from triangular_arbitrage.exchange.ccxt_rest import load_spot_markets
from triangular_arbitrage.exchange.common import (
    BACKOFF_MAX,
    BACKOFF_START,
    RECV_TIMEOUT,
    FeedStats,
    as_version,
    chunks,
    exchange_ts_seconds,
    levels_from_rows,
)
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.htx")

WS_URL = "wss://api.huobi.pro/ws"


class HtxAdapter:
    """HTX (Huobi) spot depth via gzip JSON snapshots."""

    name = "htx"

    def __init__(self, *, book_depth: int, taker_override: float | None, subs_per_conn: int = 50) -> None:
        # step0 = no aggregation; full snapshot push
        self.book_depth = max(5, book_depth)
        self.taker_override = taker_override
        self.subs_per_conn = subs_per_conn
        self._native_to_unified: dict[str, str] = {}
        self.stats = FeedStats(mode="depth.step0")

    async def load_markets(self) -> list[Market]:
        return await load_spot_markets(self.name, self.taker_override)

    async def stream_books(
        self,
        native_by_unified: dict[str, str],
        on_snapshot: BookHandler,
        stop_event: asyncio.Event,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        # HTX native ids are usually lowercase btcusdt
        self._native_to_unified = {n.lower(): u for u, n in native_by_unified.items()}
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
        unifieds = [
            self._native_to_unified[n.lower()]
            for n in natives
            if n.lower() in self._native_to_unified
        ]
        backoff = BACKOFF_START
        while not stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                    for i, native in enumerate(natives):
                        topic = f"market.{native.lower()}.depth.step0"
                        await ws.send(json.dumps({"sub": topic, "id": f"d{i}"}))
                    while not stop_event.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                        await self._handle(raw, on_snapshot, ws)
                        backoff = BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.reconnects += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("HTX ws drop: %s", self.stats.last_error)
                if on_disconnect:
                    await on_disconnect(unifieds)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _handle(self, raw, on_snapshot, ws) -> None:
        try:
            if isinstance(raw, bytes):
                raw = gzip.decompress(raw).decode("utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.last_error = f"parse: {exc}"
            return
        if "ping" in payload:
            await ws.send(json.dumps({"pong": payload["ping"]}))
            self.stats.last_message_ts = time.time()
            return
        tick = payload.get("tick") or {}
        ch = str(payload.get("ch") or "")
        # market.btcusdt.depth.step0
        parts = ch.split(".")
        native = parts[1] if len(parts) >= 2 else ""
        unified = self._native_to_unified.get(native.lower())
        if not unified:
            return
        bids = levels_from_rows(tick.get("bids"))
        asks = levels_from_rows(tick.get("asks"))
        if not (bids and asks):
            return
        # HTX can send deep books; keep top N for our validator
        bids = sorted(bids, key=lambda x: -x[0])[: self.book_depth]
        asks = sorted(asks, key=lambda x: x[0])[: self.book_depth]
        self.stats.snapshots += 1
        self.stats.last_message_ts = time.time()
        await on_snapshot(
            unified,
            bids,
            asks,
            exchange_ts=exchange_ts_seconds(tick.get("ts") or payload.get("ts")),
            version=as_version(tick.get("version")),
        )
