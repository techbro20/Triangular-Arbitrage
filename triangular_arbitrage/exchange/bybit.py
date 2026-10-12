from __future__ import annotations

import asyncio
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
    chunks,
    exchange_ts_seconds,
)
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.bybit")

WS_URL = "wss://stream.bybit.com/v5/public/spot"


class BybitAdapter:
    """Bybit spot orderbook (snapshot + delta). Spot depths: 1/50/200/1000."""

    name = "bybit"

    def __init__(self, *, book_depth: int, taker_override: float | None, subs_per_conn: int = 10) -> None:
        self.stream_depth = 50
        self.emit_depth = max(5, min(book_depth, 50))
        self.taker_override = taker_override
        self.subs_per_conn = min(subs_per_conn, 10)
        self._native_to_unified: dict[str, str] = {}
        self._books: dict[str, dict[str, dict[float, float]]] = {}
        self.stats = FeedStats(mode=f"orderbook.{self.stream_depth}")

    async def load_markets(self) -> list[Market]:
        return await load_spot_markets(self.name, self.taker_override)

    async def stream_books(
        self,
        native_by_unified: dict[str, str],
        on_snapshot: BookHandler,
        stop_event: asyncio.Event,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
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
                async with websockets.connect(WS_URL, ping_interval=20, max_size=2**22) as ws:
                    args = [f"orderbook.{self.stream_depth}.{n}" for n in natives]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    while not stop_event.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                        await self._handle(raw, on_snapshot, ws)
                        backoff = BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.reconnects += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Bybit ws drop: %s", self.stats.last_error)
                for n in natives:
                    self._books.pop(n.upper(), None)
                if on_disconnect:
                    await on_disconnect(unifieds)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    def _update_book(self, native: str, data: dict, msg_type: str) -> tuple[list, list] | None:
        key = native.upper()
        state = self._books.setdefault(key, {"bids": {}, "asks": {}})
        replace = msg_type == "snapshot"

        def apply(side: str, rows) -> None:
            book_side = state[side]
            if replace:
                book_side.clear()
            for row in rows or []:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                price, qty = float(row[0]), float(row[1])
                if qty == 0:
                    book_side.pop(price, None)
                else:
                    book_side[price] = qty

        apply("bids", data.get("b"))
        apply("asks", data.get("a"))
        bids = sorted(((p, q) for p, q in state["bids"].items() if q > 0), key=lambda x: -x[0])[
            : self.emit_depth
        ]
        asks = sorted(((p, q) for p, q in state["asks"].items() if q > 0), key=lambda x: x[0])[
            : self.emit_depth
        ]
        if not (bids and asks):
            return None
        return bids, asks

    async def _handle(self, raw, on_snapshot, ws) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.stats.parse_errors += 1
            return
        if payload.get("op") == "ping":
            await ws.send(json.dumps({"op": "pong"}))
            self.stats.last_message_ts = time.time()
            return
        if "topic" not in payload:
            return
        topic = str(payload.get("topic") or "")
        native = topic.rsplit(".", 1)[-1]
        unified = self._native_to_unified.get(native.upper())
        if not unified:
            return
        data = payload.get("data") or {}
        msg_type = str(payload.get("type") or "snapshot")
        try:
            levels = self._update_book(native, data, msg_type)
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.last_error = f"book: {exc}"
            return
        if levels is None:
            return
        bids, asks = levels
        self.stats.snapshots += 1
        self.stats.last_message_ts = time.time()
        # No version: Bybit resets update ids on snapshot, which would look out-of-order.
        await on_snapshot(unified, bids, asks, exchange_ts=exchange_ts_seconds(payload.get("ts")))
