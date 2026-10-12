from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import aiohttp
import websockets

from triangular_arbitrage.exchange.base import BookHandler, DisconnectHandler
from triangular_arbitrage.exchange.ccxt_rest import load_spot_markets
from triangular_arbitrage.exchange.common import RECV_TIMEOUT, exchange_ts_seconds
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.kucoin")

BULLET_URL = "https://api.kucoin.com/api/v1/bullet-public"
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


@dataclass
class KucoinFeedStats:
    reconnects: int = 0
    parse_errors: int = 0
    snapshots: int = 0
    last_error: str = ""
    last_message_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "reconnects": self.reconnects,
            "parse_errors": self.parse_errors,
            "snapshots": self.snapshots,
            "last_error": self.last_error[:120],
            "last_message_age_s": round(time.time() - self.last_message_ts, 1)
            if self.last_message_ts
            else None,
        }


class KucoinAdapter:
    name = "kucoin"

    def __init__(
        self,
        *,
        symbols_per_topic: int,
        book_depth: int,
        taker_override: float | None,
    ) -> None:
        self.symbols_per_topic = symbols_per_topic
        self.book_depth = 5 if book_depth <= 5 else 50
        self.taker_override = taker_override
        self._native_to_unified: dict[str, str] = {}
        self.stats = KucoinFeedStats()

    async def load_markets(self) -> list[Market]:
        return await load_spot_markets(self.name, self.taker_override)

    async def stream_books(
        self,
        native_by_unified: dict[str, str],
        on_snapshot: BookHandler,
        stop_event: asyncio.Event,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        self._native_to_unified = {native.upper(): unified for unified, native in native_by_unified.items()}
        groups = _chunks(list(native_by_unified.values()), self.symbols_per_topic)
        tasks = [
            asyncio.create_task(self._run_connection(group, on_snapshot, stop_event, on_disconnect))
            for group in groups
        ]
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _bullet(self) -> tuple[str, float]:
        async with aiohttp.ClientSession() as session:
            async with session.post(BULLET_URL) as resp:
                payload = await resp.json()
        data = payload["data"]
        server = data["instanceServers"][0]
        token = data["token"]
        endpoint = server["endpoint"]
        ping_ms = float(server.get("pingInterval") or 18000)
        connect_id = str(int(time.time() * 1000))
        url = f"{endpoint}?token={token}&connectId={connect_id}"
        return url, ping_ms / 1000.0

    async def _run_connection(
        self,
        natives: list[str],
        on_snapshot: BookHandler,
        stop_event: asyncio.Event,
        on_disconnect: DisconnectHandler | None,
    ) -> None:
        topic_prefix = "/spotMarket/level2Depth5" if self.book_depth == 5 else "/spotMarket/level2Depth50"
        topic = f"{topic_prefix}:{','.join(natives)}"
        unifieds = [
            self._native_to_unified[n.upper()]
            for n in natives
            if n.upper() in self._native_to_unified
        ]
        backoff = _BACKOFF_START

        while not stop_event.is_set():
            try:
                url, ping_every = await self._bullet()
                async with websockets.connect(
                    url,
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=2**23,
                    close_timeout=5,
                ) as ws:
                    sub = {
                        "id": str(int(time.time() * 1000)),
                        "type": "subscribe",
                        "topic": topic,
                        "privateChannel": False,
                        "response": True,
                    }
                    await ws.send(json.dumps(sub))
                    ping_task = asyncio.create_task(self._ping(ws, ping_every, stop_event))
                    try:
                        while not stop_event.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=max(RECV_TIMEOUT, ping_every + 15))
                            await self._handle(raw, on_snapshot, ws)
                            backoff = _BACKOFF_START
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.reconnects += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("KuCoin ws drop backoff=%.1fs err=%s", backoff, self.stats.last_error)
                if on_disconnect is not None:
                    try:
                        await on_disconnect(unifieds)
                    except Exception:
                        log.exception("on_disconnect failed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _handle(self, raw, on_snapshot: BookHandler, ws) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.last_error = f"parse: {exc}"
            return
        msg_type = payload.get("type")
        if msg_type == "ping":
            await ws.send(json.dumps({"id": payload.get("id"), "type": "pong"}))
            self.stats.last_message_ts = time.time()
            return
        if msg_type != "message":
            self.stats.last_message_ts = time.time()
            return
        topic = payload.get("topic") or ""
        data = payload.get("data") or {}
        native = topic.split(":")[-1] if ":" in topic else ""
        if "," in native:
            # multi-symbol topic; prefer symbol inside data if present
            native = str(data.get("symbol") or native.split(",")[0])
        if not native and isinstance(data, dict):
            native = str(data.get("symbol") or "")
        unified = self._native_to_unified.get(native.upper())
        if not unified:
            return
        try:
            bids = [(float(p), float(q)) for p, q in data.get("bids") or []]
            asks = [(float(p), float(q)) for p, q in data.get("asks") or []]
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.last_error = f"levels: {exc}"
            return
        if not (bids and asks):
            return
        self.stats.snapshots += 1
        self.stats.last_message_ts = time.time()
        await on_snapshot(
            unified,
            bids,
            asks,
            exchange_ts=exchange_ts_seconds(data.get("timestamp")),
        )

    async def _ping(self, ws, every: float, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await ws.send(json.dumps({"id": str(int(time.time() * 1000)), "type": "ping"}))
            except Exception:
                return
            await asyncio.sleep(max(every / 2, 5))
