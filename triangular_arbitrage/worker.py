from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from ccxt.base import errors as ccxt_errors

from triangular_arbitrage.books import BookStore
from triangular_arbitrage.config import Settings
from triangular_arbitrage.context import AppContext
from triangular_arbitrage.execution import LiveExecutor, PaperExecutor
from triangular_arbitrage.exchange.bingx import BingxAdapter
from triangular_arbitrage.exchange.bybit import BybitAdapter
from triangular_arbitrage.exchange.ccxt_rest import RestBookClient
from triangular_arbitrage.exchange.gateio import GateioAdapter
from triangular_arbitrage.exchange.htx import HtxAdapter
from triangular_arbitrage.exchange.kucoin import KucoinAdapter
from triangular_arbitrage.exchange.mexc import MexcAdapter
from triangular_arbitrage.fees import resolve_fees
from triangular_arbitrage.graph import enumerate_cycles, index_cycles, select_cycles, volume_map
from triangular_arbitrage.inventory import Inventory
from triangular_arbitrage.models import (
    Cycle,
    Opportunity,
    RejectReason,
    RejectedOpportunity,
    WorkerSnapshot,
)
from triangular_arbitrage.validator import check_sync, top_of_book_product, validate_cycle

log = logging.getLogger("arb.worker")


def build_adapter(name: str, settings: Settings):
    fee = settings.fee_overrides.get(name)
    if name == "mexc":
        return MexcAdapter(
            ws_url=settings.mexc_ws_url,
            legacy_url=settings.mexc_ws_legacy_url,
            subs_per_conn=settings.mexc_subs_per_conn,
            book_depth=settings.book_depth,
            taker_override=fee,
        )
    if name == "kucoin":
        return KucoinAdapter(
            symbols_per_topic=settings.kucoin_symbols_per_topic,
            book_depth=settings.book_depth,
            taker_override=fee,
        )
    if name == "gateio":
        return GateioAdapter(book_depth=settings.book_depth, taker_override=fee)
    if name == "htx":
        return HtxAdapter(book_depth=settings.book_depth, taker_override=fee)
    if name == "bingx":
        return BingxAdapter(book_depth=settings.book_depth, taker_override=fee)
    if name == "bybit":
        return BybitAdapter(book_depth=settings.book_depth, taker_override=fee)
    if name == "bitmart":
        raise ValueError(
            "bitmart is unavailable: CCXT delisted it and BitMart is winding down trading"
        )
    raise ValueError(
        f"unsupported exchange {name}; supported: mexc,kucoin,gateio,htx,bingx,bybit"
    )


def _format_trade_alert(exchange: str, opportunity: Opportunity, path: str) -> str:
    sign = "+" if opportunity.net_profit >= 0 else ""
    return (
        f"TRADE  ·  {exchange.upper()}\n"
        f"{sign}{opportunity.net_profit:.4f} {opportunity.cycle.start_asset}  "
        f"({opportunity.net_profit_bps:.1f} bps)\n"
        f"Size  {opportunity.start_amount:.4f} → {opportunity.end_amount:.4f}\n"
        f"{path}"
    )


def _mid(bids, asks) -> float | None:
    if not bids or not asks:
        return None
    return (bids[0][0] + asks[0][0]) / 2.0


_NETWORK_ERRORS = (ccxt_errors.NetworkError, aiohttp.ClientError, OSError)
_OUTAGE_BACKOFF_START = 5.0
_OUTAGE_BACKOFF_MAX = 300.0
# A session that survived this long counts as healthy, so the next outage starts a fresh backoff.
_HEALTHY_SESSION_S = 120.0


async def run_worker(exchange: str, ctx: AppContext) -> None:
    backoff = _OUTAGE_BACKOFF_START
    outage_alerted = False
    while not ctx.stop.is_set() and not ctx.risk.killed:
        started = time.monotonic()
        try:
            await _run_session(exchange, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.statuses.setdefault(exchange, WorkerSnapshot(exchange=exchange)).errors += 1
            if time.monotonic() - started >= _HEALTHY_SESSION_S:
                backoff = _OUTAGE_BACKOFF_START
                outage_alerted = False
            if isinstance(exc, _NETWORK_ERRORS):
                log.warning(
                    "%s network unavailable (%s: %s); retrying in %.0fs",
                    exchange,
                    type(exc).__name__,
                    str(exc)[:200],
                    backoff,
                )
                if not outage_alerted:
                    outage_alerted = True
                    await ctx.alerts.send(
                        f"{exchange.upper()} network outage ({type(exc).__name__}); "
                        f"retrying with backoff up to {_OUTAGE_BACKOFF_MAX:.0f}s"
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _OUTAGE_BACKOFF_MAX)
                continue
            log.exception("%s worker crashed", exchange)
            await ctx.alerts.send(f"{exchange} worker error; retrying in 5s")
            await asyncio.sleep(5)


async def _run_session(exchange: str, ctx: AppContext) -> None:
    settings = ctx.settings
    status = ctx.statuses.setdefault(exchange, WorkerSnapshot(exchange=exchange))
    adapter = build_adapter(exchange, settings)
    markets = await adapter.load_markets()
    markets, status.fees = await resolve_fees(exchange, markets, settings)
    cycles = enumerate_cycles(markets, settings.start_asset_list, settings.max_cycle_length)
    selected, symbols = select_cycles(
        cycles,
        settings.max_symbols,
        volume_by_market=volume_map(markets),
        prefer_mid_low=settings.prefer_mid_low_liquidity,
        min_quote_volume=settings.min_quote_volume,
        max_quote_volume=settings.max_quote_volume,
        skip_low_quality=settings.skip_low_quality_cycles,
    )
    if not selected:
        await ctx.alerts.send(f"{exchange}: no 3/4-cycles in start assets {settings.start_asset_list}")
        await ctx.stop.wait()
        return

    by_unified = {market.unified: market for market in markets if market.unified in symbols}
    native_by_unified = {unified: market.native_id for unified, market in by_unified.items()}
    cycle_index = index_cycles(selected)
    status.subscribed = len(native_by_unified)
    status.cycles = len(selected)
    parts = []
    if settings.prefer_mid_low_liquidity:
        parts.append("mid-low liq")
    if settings.skip_low_quality_cycles:
        parts.append("skip junk")
    mode = "+".join(parts) if parts else "default"
    await ctx.alerts.send(
        f"{exchange.upper()} ready  ·  {len(selected)} cycles  ·  {len(native_by_unified)} books  ·  {mode}"
    )

    books = BookStore()
    balances = settings.paper_balances_map().get(exchange, {"USDT": 1000.0})
    inventory = Inventory(balances)
    min_bps = settings.min_bps_for(exchange)
    status.min_bps = min_bps
    cycle_cooldown = max(settings.alert_cooldown_seconds, settings.pair_cooldown_seconds)
    if settings.live_enabled:
        executor = LiveExecutor()
    else:
        executor = PaperExecutor(
            books,
            leg_latency_ms=settings.leg_latency_ms,
            usage_frac=settings.book_usage_frac,
            stale_after=settings.book_stale_seconds,
            consumed_hold_seconds=max(settings.consumed_hold_seconds, cycle_cooldown + 5.0),
            min_net_profit_bps=min_bps,
        )
    rest = RestBookClient(exchange) if settings.rest_verify_enabled else None
    warm_task = asyncio.create_task(rest.warm()) if rest else None

    pending: set[str] = set()
    lock = asyncio.Lock()
    min_product = 1.0 + min_bps / 10_000.0
    status.blocked_cycles.update(ctx.history.active_blocks(exchange))
    # cycle id -> blocked until (alert cooldown after a fill, fade cooldown after a miss)
    cycle_block: dict[str, float] = dict(status.blocked_cycles)
    # market -> blocked until; set on every pair a fill touched
    pair_block: dict[str, float] = {}
    busy: set[str] = set()
    reject_logged: dict[str, float] = {}
    tasks: set[asyncio.Task] = set()

    def validate(cycle: Cycle, store: BookStore) -> Opportunity | RejectedOpportunity:
        return validate_cycle(
            cycle,
            store,
            start_balance=inventory.get(cycle.start_asset),
            min_notional=max(settings.min_notional, 0.0),
            max_notional=settings.max_notional,
            min_net_profit_bps=min_bps,
            usage_frac=settings.book_usage_frac,
            stale_after=settings.book_stale_seconds,
            min_fill_notional_frac=settings.min_fill_notional_frac,
        )

    def miss(cycle: Cycle, reason: RejectReason, theoretical: float, detail: str, counter: str) -> None:
        status.bump(counter)
        status.last_reject = f"{reason.value} {detail}"
        cycle_block[cycle.id] = time.time() + settings.fade_cooldown_seconds
        ctx.opp_log.write_rejected(exchange, RejectedOpportunity(cycle, reason, theoretical, detail))

    async def count_miss(cycle: Cycle, kind: str, profit: float | None) -> None:
        """Slipped/failed fill or REST book mismatch: extend the streak, block with escalation."""
        now = time.time()
        streak = ctx.history.record_miss(exchange, cycle.id, kind, profit, now)
        if settings.cycle_miss_limit <= 0 or streak < settings.cycle_miss_limit:
            return
        until, hours = ctx.history.block(
            exchange, cycle.id, now, settings.cycle_block_hours, settings.cycle_block_max_hours
        )
        cycle_block[cycle.id] = until
        status.blocked_cycles[cycle.id] = until
        status.bump("auto_blocked")
        log.info("%s auto-blocked %s for %.1fh after %s misses", exchange, cycle.id, hours, streak)
        await ctx.alerts.send(
            f"{exchange.upper()} blocked {cycle.id} for {hours:g}h "
            f"({streak} slipped/failed/REST mismatches in a row)"
        )

    async def on_snapshot(unified: str, bids, asks, *, exchange_ts=None, version=None) -> None:
        if not books.apply_snapshot(unified, bids, asks, exchange_ts=exchange_ts, version=version):
            status.bump("out_of_order")
            return
        status.books_ready = books.ready_count()
        status.last_book_ts = time.time()
        async with lock:
            pending.add(unified)

    async def on_disconnect(unifieds) -> None:
        books.invalidate_many(list(unifieds))
        status.books_ready = books.ready_count()
        log.info("%s invalidated %s books after disconnect", exchange, len(unifieds))

    async def rest_verify(cycle: Cycle, confirmed: Opportunity) -> Opportunity | None:
        if rest is None:
            return confirmed
        if not rest.ready:
            miss(cycle, RejectReason.REST_MISMATCH, confirmed.theoretical_product, "rest not ready", "rest_mismatch")
            return None
        try:
            fetched = await rest.fetch_books(list(cycle.markets), settings.book_depth)
        except Exception as exc:
            miss(cycle, RejectReason.REST_MISMATCH, confirmed.theoretical_product, f"rest error {exc!r}"[:120], "rest_mismatch")
            return None
        rest_books = BookStore()
        diverged: list[str] = []
        for market, (bids, asks) in fetched.items():
            rest_books.apply_snapshot(market, bids, asks)
            ws_book = books.get(market)
            ws_mid = _mid(ws_book.bids, ws_book.asks) if ws_book else None
            rest_mid = _mid(bids, asks)
            if ws_mid and rest_mid and abs(ws_mid / rest_mid - 1.0) * 10_000 > settings.rest_divergence_bps:
                diverged.append(market)
        checked = validate(cycle, rest_books)
        if isinstance(checked, RejectedOpportunity):
            if diverged:
                # The websocket book disagrees with the exchange: drop it until the next snapshot
                books.invalidate_many(diverged)
            detail = f"ws {confirmed.net_profit_bps:.1f}bps, rest {checked.reason.value}"
            if diverged:
                detail += f" (diverged: {','.join(diverged)})"
            miss(cycle, RejectReason.REST_MISMATCH, confirmed.theoretical_product, detail, "rest_mismatch")
            await count_miss(cycle, "rest_mismatch", None)
            return None
        return confirmed if confirmed.start_amount <= checked.start_amount else checked

    async def handle_candidate(cycle: Cycle, first: Opportunity) -> None:
        try:
            if settings.confirm_ms > 0:
                await asyncio.sleep(settings.confirm_ms / 1000.0)

            sync = check_sync(
                cycle,
                books,
                stale_after=settings.book_stale_seconds,
                max_feed_lag_s=settings.max_feed_lag_ms / 1000.0,
                max_leg_skew_s=settings.max_leg_skew_ms / 1000.0,
            )
            if not sync.ok:
                miss(cycle, sync.reason or RejectReason.OUT_OF_SYNC, first.theoretical_product, sync.detail, "out_of_sync")
                return

            second = validate(cycle, books)
            if isinstance(second, RejectedOpportunity):
                detail = f"{first.net_profit_bps:.1f}bps -> {second.reason.value} after {settings.confirm_ms:.0f}ms"
                miss(cycle, RejectReason.FADED, first.theoretical_product, detail, "faded")
                return

            if settings.rest_verify_enabled and (
                second.net_profit_bps > settings.rest_verify_bps or sync.suspicious
            ):
                status.bump("rest_checks")
                verified = await rest_verify(cycle, second)
                if verified is None:
                    return
                second = verified

            status.bump("confirmed")
            result = await executor.execute(second, inventory, start_amount=second.start_amount)
            now = time.time()
            for market in cycle.markets:
                pair_block[market] = now + settings.pair_cooldown_seconds
            cycle_block[cycle.id] = now + cycle_cooldown
            status.bump(result.status)

            if result.status != "held" or result.realised is None:
                status.last_reject = f"{result.status} {result.detail}"
                ctx.opp_log.write_execution_miss(exchange, result)
                realised_profit = result.realised.net_profit if result.realised is not None else None
                if realised_profit is not None:
                    status.slipped_pnl += realised_profit
                    status.honest_pnl += realised_profit
                await count_miss(cycle, result.status, realised_profit)
                return

            filled = result.realised
            status.honest_pnl += filled.net_profit
            ctx.history.record_booked(exchange, cycle.id, filled.net_profit, now)
            was_paused = ctx.risk.is_paused(exchange)
            ctx.risk.record_pnl(exchange, filled.net_profit)
            trade = ctx.trades.record(exchange, filled)
            status.paper_pnl = ctx.risk.session_pnl(exchange)
            status.last_accept = f"{filled.net_profit_bps:.1f}bps"
            status.paused = ctx.risk.is_paused(exchange)
            ctx.opp_log.write_accepted(exchange, filled)
            await ctx.alerts.send(_format_trade_alert(exchange, filled, trade.path))
            if not was_paused and ctx.risk.is_paused(exchange):
                await ctx.alerts.send(f"{exchange.upper()} paused — session loss limit")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s candidate %s failed", exchange, cycle.id)
            status.errors += 1
        finally:
            busy.difference_update(cycle.markets)
            ctx.risk.release(exchange)

    def process(batch: set[str]) -> None:
        if ctx.risk.killed:
            ctx.stop.set()
            return
        if ctx.risk.is_paused(exchange):
            status.paused = True
            return
        status.paused = False
        now = time.time()
        seen: set[str] = set()
        for unified in batch:
            for cycle in cycle_index.get(unified, []):
                if cycle.id in seen:
                    continue
                seen.add(cycle.id)
                theoretical = top_of_book_product(cycle, books, settings.book_stale_seconds)
                if theoretical is None or theoretical < min_product:
                    continue
                if cycle_block.get(cycle.id, 0.0) > now:
                    continue
                if any(pair_block.get(m, 0.0) > now or m in busy for m in cycle.markets):
                    continue
                result = validate(cycle, books)
                if isinstance(result, RejectedOpportunity):
                    theoretical_bps = (theoretical - 1.0) * 10_000
                    status.last_reject = f"{result.reason.value} {theoretical_bps:.1f}bps"
                    if (
                        theoretical_bps >= settings.log_reject_product_bps
                        and now - reject_logged.get(cycle.id, 0.0) >= settings.reject_log_seconds
                    ):
                        reject_logged[cycle.id] = now
                        ctx.opp_log.write_rejected(exchange, result)
                    continue
                blocked = ctx.risk.allow(exchange, result.start_amount)
                if blocked:
                    if blocked != RejectReason.RISK_LIMIT or ctx.risk.in_flight_by.get(exchange, 0) == 0:
                        ctx.opp_log.write_rejected(
                            exchange,
                            RejectedOpportunity(cycle, blocked, theoretical, blocked.value),
                        )
                    continue
                status.bump("candidates")
                ctx.risk.acquire(exchange)
                busy.update(cycle.markets)
                task = asyncio.create_task(handle_candidate(cycle, result))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    async def pump() -> None:
        interval = max(settings.coalesce_ms, 1) / 1000.0
        while not ctx.stop.is_set() and not ctx.risk.killed:
            await asyncio.sleep(interval)
            if hasattr(adapter, "stats"):
                status.extra = adapter.stats.as_dict()
            async with lock:
                batch = set(pending)
                pending.clear()
            if batch:
                try:
                    process(batch)
                except Exception:
                    log.exception("%s process failed", exchange)
                    status.errors += 1

    stream_task = asyncio.create_task(
        adapter.stream_books(native_by_unified, on_snapshot, ctx.stop, on_disconnect=on_disconnect)
    )
    pump_task = asyncio.create_task(pump())
    try:
        await ctx.stop.wait()
    finally:
        background = [stream_task, pump_task, *tasks]
        if warm_task:
            background.append(warm_task)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        # tasks cancelled before they started never ran their release()
        ctx.risk.in_flight_by[exchange.lower()] = 0
        if rest:
            await rest.close()
