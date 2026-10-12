from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import websockets

from triangular_arbitrage.exchange.base import BookHandler, DisconnectHandler
from triangular_arbitrage.exchange.ccxt_rest import load_spot_markets
from triangular_arbitrage.exchange.common import as_version, exchange_ts_seconds
from triangular_arbitrage.exchange.mexc_proto import parse_mexc_limit_depth_full
from triangular_arbitrage.models import Market

log = logging.getLogger("arb.mexc")

_PING = json.dumps({"method": "PING"})
_LEGACY_AFTER_FAILURES = 5
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
_RECV_TIMEOUT = 120.0
_PING_EVERY = 15.0


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def _channel(native_id: str, depth: int) -> str:
    return f"spot@public.limit.depth.v3.api.pb@{native_id}@{depth}"


def _legacy_channel(native_id: str, depth: int) -> str:
    return f"spot@public.limit.depth.v3.api@{native_id}@{depth}"


def _parse_json_depth(payload: dict) -> tuple[str | None, list[tuple[float, float]], list[tuple[float, float]]]:
    data = payload.get("d") or payload.get("publicLimitDepths") or payload.get("data") or payload
    symbol = payload.get("s") or payload.get("symbol")
    if isinstance(data, dict):
        symbol = symbol or data.get("symbol")
        raw_bids = data.get("bids") or []
        raw_asks = data.get("asks") or []
    else:
        raw_bids, raw_asks = [], []

    def levels(rows) -> list[tuple[float, float]]:
        out = []
        for row in rows:
            if isinstance(row, dict):
                price = row.get("p") or row.get("price")
                qty = row.get("v") or row.get("quantity")
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                price, qty = row[0], row[1]
            else:
                continue
            out.append((float(price), float(qty)))
        return out

    return symbol, levels(raw_bids), levels(raw_asks)


@dataclass
class MexcFeedStats:
    reconnects: int = 0
    parse_errors: int = 0
    snapshots: int = 0
    sub_acks: int = 0
    mode: str = "protobuf"
    last_error: str = ""
    last_message_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "reconnects": self.reconnects,
            "parse_errors": self.parse_errors,
            "snapshots": self.snapshots,
            "sub_acks": self.sub_acks,
            "mode": self.mode,
            "last_error": self.last_error[:120],
            "last_message_age_s": round(time.time() - self.last_message_ts, 1)
            if self.last_message_ts
            else None,
        }


class MexcAdapter:
    name = "mexc"

    def __init__(
        self,
        *,
        ws_url: str,
        legacy_url: str,
        subs_per_conn: int,
        book_depth: int,
        taker_override: float | None,
    ) -> None:
        self.ws_url = ws_url
        self.legacy_url = legacy_url
        self.subs_per_conn = subs_per_conn
        self.book_depth = 5 if book_depth not in (5, 10, 20) else book_depth
        self.taker_override = taker_override
        self._native_to_unified: dict[str, str] = {}
        self.stats = MexcFeedStats()
        self._lock = asyncio.Lock()

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
        natives = list(native_by_unified.values())
        groups = _chunks(natives, self.subs_per_conn)
        tasks = [
            asyncio.create_task(
                self._run_connection(group, on_snapshot, stop_event, on_disconnect)
            )
            for group in groups
        ]
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_connection(
        self,
        natives: list[str],
        on_snapshot: BookHandler,
        stop_event: asyncio.Event,
        on_disconnect: DisconnectHandler | None,
    ) -> None:
        use_legacy = False
        consecutive_failures = 0
        backoff = _BACKOFF_START
        unifieds = [
            self._native_to_unified[n.upper()]
            for n in natives
            if n.upper() in self._native_to_unified
        ]

        while not stop_event.is_set():
            url = self.legacy_url if use_legacy else self.ws_url
            channels = [
                _legacy_channel(native, self.book_depth) if use_legacy else _channel(native, self.book_depth)
                for native in natives
            ]
            self.stats.mode = "legacy" if use_legacy else "protobuf"
            session_ok = False
            try:
                async with websockets.connect(
                    url,
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=2**23,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps({"method": "SUBSCRIPTION", "params": channels}))
                    ping_task = asyncio.create_task(self._ping(ws, stop_event))
                    try:
                        while not stop_event.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT)
                            handled = await self._handle_message(raw, on_snapshot, ws)
                            if handled:
                                session_ok = True
                                consecutive_failures = 0
                                backoff = _BACKOFF_START
                    finally:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                self.stats.reconnects += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "MEXC ws drop mode=%s fail=%s backoff=%.1fs err=%s",
                    self.stats.mode,
                    consecutive_failures,
                    backoff,
                    self.stats.last_error,
                )
                if on_disconnect is not None:
                    try:
                        await on_disconnect(unifieds)
                    except Exception:
                        log.exception("on_disconnect failed")

                if not use_legacy and consecutive_failures >= _LEGACY_AFTER_FAILURES:
                    use_legacy = True
                    consecutive_failures = 0
                    log.warning("MEXC falling back to legacy JSON after %s protobuf failures", _LEGACY_AFTER_FAILURES)
                elif use_legacy and consecutive_failures >= _LEGACY_AFTER_FAILURES:
                    # try protobuf again after legacy also struggles
                    use_legacy = False
                    consecutive_failures = 0
                    log.warning("MEXC retrying protobuf after legacy failures")

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue

            if session_ok and not stop_event.is_set():
                # clean exit of recv loop without exception — still reconnect
                self.stats.reconnects += 1
                if on_disconnect is not None:
                    try:
                        await on_disconnect(unifieds)
                    except Exception:
                        log.exception("on_disconnect failed")
                await asyncio.sleep(backoff)

    async def _handle_message(self, raw, on_snapshot: BookHandler, ws) -> bool:
        try:
            if isinstance(raw, bytes):
                if raw[:1] in (b"{", b"["):
                    return await self._handle_text(raw.decode("utf-8"), on_snapshot, ws)
                try:
                    depth = parse_mexc_limit_depth_full(raw)
                except Exception as exc:
                    self.stats.parse_errors += 1
                    self.stats.last_error = f"pb_parse: {exc}"
                    log.debug("MEXC protobuf parse error: %s", exc)
                    return False
                return await self._emit(
                    depth.symbol,
                    depth.bids,
                    depth.asks,
                    on_snapshot,
                    exchange_ts=exchange_ts_seconds(depth.send_time_ms),
                    version=depth.version,
                )
            return await self._handle_text(raw, on_snapshot, ws)
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.last_error = f"handle: {exc}"
            log.debug("MEXC handle error: %s", exc)
            return False

    async def _handle_text(self, text: str, on_snapshot: BookHandler, ws) -> bool:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.stats.parse_errors += 1
            return False
        if not isinstance(payload, dict):
            return False

        msg = str(payload.get("msg", ""))
        method = str(payload.get("method", "")).upper()
        if method == "PING" or msg.upper() == "PING":
            await ws.send(json.dumps({"method": "PONG"}))
            self.stats.last_message_ts = time.time()
            return True

        # subscription / pong acks
        if payload.get("code") == 0:
            self.stats.sub_acks += 1
            self.stats.last_message_ts = time.time()
            # may be ack only
            if "asks" not in payload and "bids" not in payload and "d" not in payload and "publicLimitDepths" not in payload:
                if "asks" not in msg.lower() and "bids" not in msg.lower():
                    return True

        if msg.upper() == "PONG":
            self.stats.last_message_ts = time.time()
            return True

        symbol, bids, asks = _parse_json_depth(payload)
        data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
        return await self._emit(
            symbol,
            bids,
            asks,
            on_snapshot,
            exchange_ts=exchange_ts_seconds(payload.get("t")),
            version=as_version(data.get("r")),
        )

    async def _emit(
        self,
        symbol: str | None,
        bids,
        asks,
        on_snapshot: BookHandler,
        *,
        exchange_ts: float | None = None,
        version: int | None = None,
    ) -> bool:
        if not symbol or not (bids and asks):
            return False
        unified = self._native_to_unified.get(symbol.upper())
        if not unified:
            return False
        self.stats.snapshots += 1
        self.stats.last_message_ts = time.time()
        await on_snapshot(unified, bids, asks, exchange_ts=exchange_ts, version=version)
        return True

    async def _ping(self, ws, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await ws.send(_PING)
            except Exception:
                return
            await asyncio.sleep(_PING_EVERY)
