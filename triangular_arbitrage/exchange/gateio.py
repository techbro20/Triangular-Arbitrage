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
    as_version,
    chunks,
    exchange_ts_seconds,
    levels_from_rows,
)
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.gateio")

WS_URL = "wss://api.gateio.ws/ws/v4/"


class GateioAdapter:
    name = "gateio"

    def __init__(self, *, book_depth: int, taker_override: float | None, subs_per_conn: int = 40) -> None:
        self.book_depth = 5 if book_depth not in (5, 10, 20, 50, 100) else book_depth
        self.taker_override = taker_override
        self.subs_per_conn = subs_per_conn
        self._native_to_unified: dict[str, str] = {}
        self.stats = FeedStats(mode="spot.order_book")

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
                async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                    for native in natives:
                        msg = {
                            "time": int(time.time()),
                            "channel": "spot.order_book",
                            "event": "subscribe",
                            "payload": [native, str(self.book_depth), "100ms"],
                        }
                        await ws.send(json.dumps(msg))
                    ping_task = asyncio.create_task(self._ping(ws, stop_event))
                    try:
                        while not stop_event.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                            await self._handle(raw, on_snapshot, ws)
                            backoff = BACKOFF_START
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.reconnects += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Gate.io ws drop: %s", self.stats.last_error)
                if on_disconnect:
                    await on_disconnect(unifieds)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _ping(self, ws, stop_event) -> None:
        while not stop_event.is_set():
            try:
                await ws.send(json.dumps({"time": int(time.time()), "channel": "spot.ping"}))
            except Exception:
                return
            await asyncio.sleep(15)

    async def _handle(self, raw, on_snapshot, ws) -> None:
        if isinstance(raw, bytes):
            # Newer Gate channels may push SBE; skip non-JSON frames.
            if raw[:1] not in (b"{", b"["):
                self.stats.parse_errors += 1
                return
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.stats.parse_errors += 1
            return
        channel = payload.get("channel")
        if channel == "spot.pong":
            self.stats.last_message_ts = time.time()
            return
        if payload.get("event") in {"subscribe", "unsubscribe"}:
            return
        if channel != "spot.order_book" or payload.get("event") != "update":
            return
        result = payload.get("result") or {}
        native = str(result.get("s") or result.get("currency_pair") or "")
        unified = self._native_to_unified.get(native.upper())
        if not unified:
            return
        bids = levels_from_rows(result.get("bids"))
        asks = levels_from_rows(result.get("asks"))
        if not (bids and asks):
            return
        self.stats.snapshots += 1
        self.stats.last_message_ts = time.time()
        await on_snapshot(
            unified,
            bids,
            asks,
            exchange_ts=exchange_ts_seconds(result.get("t") or payload.get("time_ms")),
            version=as_version(result.get("lastUpdateId")),
        )
