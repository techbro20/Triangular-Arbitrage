from __future__ import annotations

import asyncio
import logging
import signal
import sys

from triangular_arbitrage.alerts import AlertHub
from triangular_arbitrage.config import Settings
from triangular_arbitrage.context import AppContext
from triangular_arbitrage.history import load_history
from triangular_arbitrage.opp_log import OpportunityLog
from triangular_arbitrage.risk import RiskManager
from triangular_arbitrage.telegram_bot import run_telegram
from triangular_arbitrage.worker import run_worker

log = logging.getLogger("arb")


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    if settings.live_enabled:
        log.error("Refusing to start: live mode is a later phase. Use TRADING_MODE=paper.")
        raise SystemExit(2)

    stop = asyncio.Event()
    history, recent = load_history(
        settings.opportunities_log,
        miss_limit=settings.cycle_miss_limit,
        block_hours=settings.cycle_block_hours,
        block_max_hours=settings.cycle_block_max_hours,
        since_ts=settings.pnl_history_since_ts,
    )
    ctx = AppContext(
        settings=settings,
        risk=RiskManager(settings),
        alerts=AlertHub(),
        opp_log=OpportunityLog(settings.opportunities_log),
        stop=stop,
        history=history,
    )
    ctx.trades.seed(recent)
    total, count = history.total_booked()
    log.info(
        "Restored history from %s: %s trades, booked %+.4f, honest %+.4f",
        settings.opportunities_log,
        count,
        total,
        history.total_honest(),
    )

    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        log.info("shutdown requested")
        ctx.risk.kill()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows often lacks add_signal_handler for these
            pass

    tasks = [
        asyncio.create_task(ctx.alerts.run(stop), name="alerts"),
        asyncio.create_task(run_telegram(ctx), name="telegram"),
    ]

    async def _start_workers() -> None:
        for i, exchange in enumerate(settings.exchange_list):
            if i:
                await asyncio.sleep(2.0)  # stagger market loads / WS connect storms
            if stop.is_set() or ctx.risk.killed:
                return
            tasks.append(asyncio.create_task(run_worker(exchange, ctx), name=f"worker-{exchange}"))

    tasks.append(asyncio.create_task(_start_workers(), name="worker-starter"))

    await ctx.alerts.send(f"Paper bot started  ·  {', '.join(settings.exchange_list)}")
    try:
        await stop.wait()
    except asyncio.CancelledError:
        stop.set()
        raise
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if sys.platform.startswith("win") and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
