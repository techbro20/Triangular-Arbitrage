from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, Message

from triangular_arbitrage.context import AppContext

log = logging.getLogger("arb.telegram")

# Same as BotFather → /setcommands (shows in Telegram's "/" menu)
BOT_COMMANDS = [
    BotCommand(command="start", description="Help"),
    BotCommand(command="status", description="Overview of all exchanges"),
    BotCommand(command="pnl", description="Paper PnL (all-time + session)"),
    BotCommand(command="trades", description="Recent paper fills"),
    BotCommand(command="debug", description="Feed / reject details"),
    BotCommand(command="pause", description="Pause trading [exchange]"),
    BotCommand(command="resume", description="Resume trading [exchange]"),
    BotCommand(command="mode", description="Paper vs live mode"),
    BotCommand(command="kill", description="Stop the bot"),
]


def _register_chat(ctx: AppContext, chat_id: int) -> None:
    ids = set(ctx.settings.chat_id_list)
    ids.add(chat_id)
    ctx.settings.telegram_chat_ids = ",".join(str(i) for i in sorted(ids))


def _allowed(message: Message, ctx: AppContext) -> bool:
    allowed_ids = ctx.settings.chat_id_list
    if not allowed_ids:
        return True
    return message.chat.id in allowed_ids


def _status_text(ctx: AppContext) -> str:
    mode = "paper" if not ctx.settings.live_enabled else "LIVE"
    state = "KILLED" if ctx.risk.killed else ("PAUSED" if ctx.risk.paused_global else "running")
    lines = [f"Bot  ·  {mode}  ·  {state}", ""]
    if not ctx.statuses:
        lines.append("Workers starting…")
        return "\n".join(lines)

    for name, snap in sorted(ctx.statuses.items()):
        paused = "paused" if ctx.risk.is_paused(name) else "ok"
        books = f"{snap.books_ready}/{snap.subscribed}"
        lines.append(f"{name.upper()}")
        lines.append(f"  state   {paused}")
        lines.append(f"  books   {books} ready")
        lines.append(f"  cycles  {snap.cycles}")
        row = ctx.history.get(name)
        lines.append(f"  pnl     {snap.paper_pnl:+.4f} session  ·  {row.booked_pnl:+.4f} all-time")
        lines.append(f"  trades  {ctx.trades.count_for(name)} session  ·  {row.booked_trades} all-time")
        if snap.last_accept:
            lines.append(f"  last    {snap.last_accept}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _pnl_text(ctx: AppContext) -> str:
    history = ctx.history
    exchanges = sorted(
        set(ctx.statuses) | set(ctx.risk.daily_pnl) | set(ctx.trades.counts) | set(history.exchanges)
    )
    if not exchanges:
        return "No activity yet."

    since = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(history.first_ts))
        if history.first_ts
        else "now"
    )
    lines = [f"PnL  ·  all-time (since {since})", ""]
    for name in exchanges:
        row = history.get(name)
        if not (row.booked_trades or row.slipped or row.failed):
            continue
        lines.append(f"{name.upper():8}  {row.booked_pnl:+.4f}   ({row.booked_trades} trades)")
    total, count = history.total_booked()
    lines.append(f"{'TOTAL':8}  {total:+.4f}   ({count} trades)")
    lines.append(f"{'honest':8}  {history.total_honest():+.4f}   (incl. slipped losses)")

    lines.extend(["", "This session (since restart)", ""])
    s_total = 0.0
    s_trades = 0
    for name in exchanges:
        pnl = ctx.risk.session_pnl(name)
        n = ctx.trades.count_for(name)
        if not n and not pnl:
            continue
        s_total += pnl
        s_trades += n
        lines.append(f"{name.upper():8}  {pnl:+.4f}   ({n} trades)")
    lines.append(f"{'TOTAL':8}  {s_total:+.4f}   ({s_trades} trades)")
    return "\n".join(lines)


def _trades_text(ctx: AppContext, limit: int = 10) -> str:
    rows = ctx.trades.recent(limit)
    if not rows:
        return "No paper trades yet.\nYou'll get an alert when one fills."
    lines = [f"Last {len(rows)} trades", ""]
    for row in rows:
        lines.append(row.short_line())
        lines.append(f"  {row.path}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _debug_text(ctx: AppContext) -> str:
    lines = ["Debug feed", ""]
    if not ctx.statuses:
        return "No workers."
    for name, snap in sorted(ctx.statuses.items()):
        lines.append(f"{name}")
        lines.append(f"  last_reject={snap.last_reject}")
        lines.append(f"  errors={snap.errors}")
        if snap.fees:
            lines.append(f"  fees: {snap.fees}")
        if snap.min_bps:
            lines.append(f"  floor={snap.min_bps:g}bps")
        lines.append(
            f"  honest_pnl session={snap.honest_pnl:+.4f} "
            f"(booked {snap.honest_pnl - snap.slipped_pnl:+.4f}, slipped {snap.slipped_pnl:+.4f})"
        )
        row = ctx.history.get(name)
        lines.append(
            f"  honest_pnl all-time={row.honest_pnl:+.4f} "
            f"(booked {row.booked_pnl:+.4f} x{row.booked_trades}, "
            f"slipped {row.slipped_pnl:+.4f} x{row.slipped}, failed x{row.failed})"
        )
        now = time.time()
        blocked = {cid: until for cid, until in snap.blocked_cycles.items() if until > now}
        snap.blocked_cycles = blocked
        for cid, until in sorted(blocked.items(), key=lambda kv: kv[1]):
            lines.append(f"  blocked {cid} ({(until - now) / 3600:.1f}h left)")
        if snap.counters:
            c = snap.counters
            lines.append(
                "  pipeline: "
                f"candidates={c.get('candidates', 0)} "
                f"faded={c.get('faded', 0)} "
                f"out_of_sync={c.get('out_of_sync', 0)} "
                f"rest_checks={c.get('rest_checks', 0)} "
                f"rest_mismatch={c.get('rest_mismatch', 0)} "
                f"confirmed={c.get('confirmed', 0)}"
            )
            executed = c.get("held", 0) + c.get("slipped", 0) + c.get("failed", 0)
            hold_rate = f" hold_rate={c.get('held', 0) / executed:.0%}" if executed else ""
            lines.append(
                "  fills: "
                f"held={c.get('held', 0)} slipped={c.get('slipped', 0)} "
                f"failed={c.get('failed', 0)}{hold_rate}"
            )
            if c.get("out_of_order"):
                lines.append(f"  out_of_order_msgs={c['out_of_order']}")
        if snap.extra:
            for key, value in snap.extra.items():
                lines.append(f"  {key}={value}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_router(ctx: AppContext) -> Router:
    router = Router()

    @router.message(Command("start", "help"))
    async def help_cmd(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        await message.answer("Paper arb dashboard. Use the / menu for commands.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        await message.answer(_status_text(ctx))

    @router.message(Command("pnl"))
    async def pnl(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        await message.answer(_pnl_text(ctx))

    @router.message(Command("trades"))
    async def trades(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        await message.answer(_trades_text(ctx))

    @router.message(Command("debug"))
    async def debug(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        await message.answer(_debug_text(ctx))

    @router.message(Command("pause"))
    async def pause(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        parts = (message.text or "").split()
        target = parts[1].lower() if len(parts) > 1 else None
        ctx.risk.pause(target)
        await message.answer(f"Paused {target or 'all'}.")

    @router.message(Command("resume"))
    async def resume(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        parts = (message.text or "").split()
        target = parts[1].lower() if len(parts) > 1 else None
        ctx.risk.resume(target)
        await message.answer(f"Resumed {target or 'all'}.")

    @router.message(Command("kill"))
    async def kill(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        ctx.risk.kill()
        ctx.stop.set()
        await message.answer("Kill switch on. Stopping.")

    @router.message(Command("mode"))
    async def mode(message: Message) -> None:
        if not _allowed(message, ctx):
            return
        _register_chat(ctx, message.chat.id)
        await message.answer("LIVE" if ctx.settings.live_enabled else "Paper trading (live off)")

    return router


async def run_telegram(ctx: AppContext) -> None:
    token = ctx.settings.telegram_bot_token.strip()
    if not token:
        log.info("Telegram disabled (no TELEGRAM_BOT_TOKEN)")
        await ctx.stop.wait()
        return

    bot = Bot(token)
    dp = Dispatcher()
    dp.include_router(_build_router(ctx))

    async def push(text: str) -> None:
        chat_ids = ctx.settings.chat_id_list
        if not chat_ids:
            log.debug("alert skipped (no chat ids yet); send /start")
            return
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id, text[:4000])
            except Exception:
                log.exception("telegram send failed chat_id=%s", chat_id)

    ctx.alerts.add_handler(push)

    async def stop_when_killed() -> None:
        await ctx.stop.wait()
        await dp.stop_polling()

    watcher = asyncio.create_task(stop_when_killed())
    try:
        await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
        log.info("Telegram command menu registered (%s commands)", len(BOT_COMMANDS))
        await ctx.alerts.send("Dashboard online.\nSend /start then /status")
        await dp.start_polling(bot, handle_signals=False)
    finally:
        watcher.cancel()
        await bot.session.close()
