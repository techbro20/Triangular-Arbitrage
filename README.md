# Cyclic arbitrage bot (paper-first)

Event-driven order-book arbitrage for **same-exchange** cycles (triangular and optional 4-cycles).  
Starts with pluggable native WebSocket adapters: **MEXC, KuCoin, Gate.io, HTX, BingX, Bybit**. Telegram for alerts/control.

**Live trading is refused by default.** Paper mode uses the same validation path live would use later.

## What it does

1. Load spot markets (CCXT REST, once).
2. Enumerate 3-cycles (default) from start assets (`USDT`, `USDC`).
3. Subscribe only to those symbols' partial depth over native WebSockets.
4. On each book update (coalesced): recheck only cycles that touch that symbol.
5. Validate with taker fees + order-book walk + inventory + risk limits.
6. Paper-fill, log JSONL, alert via Telegram.

Tickers / last price are **not** used.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -r requirements.txt
pip install -r dev_requirements.txt
cp .env.example .env
```

Edit `.env` (Telegram optional). Never enable withdrawal permissions on API keys when you later add live trading.

## Run (paper)

```bash
python main.py
```

Headless works without Telegram. With `TELEGRAM_BOT_TOKEN` set (send `/start` once if chat id is empty):

| Command | Action |
|---------|--------|
| `/status` | Overview: books, PnL, trade count |
| `/pnl` | Today's profit per exchange |
| `/trades` | Last paper fills |
| `/pause` / `/resume` | Pause trading |
| `/kill` | Stop workers |
| `/debug` | Feed reconnect / parse details |
| `/mode` | Confirm paper vs live |

Push alerts fire on **paper fills** and important risk events.

Logs: console + `logs/opportunities.jsonl` (accepted and high theoretical rejects).

## Tests

```bash
pytest -q
```

## Config highlights

| Env | Meaning |
|-----|---------|
| `EXCHANGES` | e.g. `mexc,kucoin,gateio,htx` (`bingx`,`bybit` also supported) |
| `MIN_NET_PROFIT_BPS` | Min net profit after fees/slippage |
| `MAX_NOTIONAL` / `MIN_NOTIONAL` | Size bounds |
| `BOOK_USAGE_FRAC` | Fraction of each depth level used |
| `DAILY_LOSS_LIMIT` | Auto-pause threshold (paper PnL) |
| `MAX_SYMBOLS` | Cap WS subscriptions per exchange |
| `PREFER_MID_LOW_LIQUIDITY` | Prefer mid/low 24h volume cycles (default true) |
| `MIN_QUOTE_VOLUME` / `MAX_QUOTE_VOLUME` | Skip dust and ultra-liquid majors when ranking |
| `PAPER_BALANCES` | JSON starting inventory per exchange |

## Architecture

```
Config + Risk
     │
     ├─ MEXC worker  (native WS books → cycles → validator → paper exec)
     ├─ KuCoin worker
     └─ Telegram + opportunity JSONL
```

## Safety

- `TRADING_MODE=paper` by default; live start exits unless both `TRADING_MODE=live` and `ENABLE_LIVE=true` (still not implemented for real orders).
- Kill switch via Telegram `/kill`.
- Daily loss limit and max notional enforced in code.

## Roadmap (not in this build)

- Native WS hardening / reconnect metrics
- OKX / Binance / Bybit adapters
- Real sequential/limit execution + private user streams
- Longer cycles (Bellman-Ford) after 3-cycles prove an edge
