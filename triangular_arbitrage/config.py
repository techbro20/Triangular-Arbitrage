from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trading_mode: str = "paper"
    enable_live: bool = False

    exchanges: str = "mexc,kucoin"
    start_assets: str = "USDT,USDC"
    max_cycle_length: int = 3
    max_symbols: int = 60

    # Cycle universe: prefer mid/low 24h quote volume (skip dust + ultra-liquid majors)
    prefer_mid_low_liquidity: bool = True
    min_quote_volume: float = 20_000.0
    max_quote_volume: float = 25_000_000.0
    # Drop stable↔stable and stables+BTC/ETH-only cycles (peg/major noise)
    skip_low_quality_cycles: bool = True

    min_net_profit_bps: float = 30.0
    # Per-exchange floor overrides: "mexc:60,bingx:25" (others use MIN_NET_PROFIT_BPS)
    min_net_profit_bps_by_exchange: str = ""
    max_notional: float = 50.0
    min_notional: float = 5.0
    # Require fills at least this fraction of MAX_NOTIONAL (drop threshold spam)
    min_fill_notional_frac: float = 0.5
    book_depth: int = 5
    book_usage_frac: float = 0.25
    book_stale_seconds: float = 15.0
    coalesce_ms: int = 25
    alert_cooldown_seconds: float = 60.0

    # Confirm-then-fill: re-score a candidate after CONFIRM_MS on fresh books, then fill
    # legs 2/3 LEG_LATENCY_MS apart on the books at that moment (live-like timing).
    confirm_ms: float = 250.0
    leg_latency_ms: float = 250.0
    # Block a cycle this long after its candidate fades or fails a check
    fade_cooldown_seconds: float = 5.0

    # Liquidity removal: paper fills hide taken size for this long (never shorter than the
    # cycle cooldown + 5s, so a repeat only books if the book really refilled), and every
    # pair a fill touched is blocked for PAIR_COOLDOWN_SECONDS.
    consumed_hold_seconds: float = 90.0
    pair_cooldown_seconds: float = 10.0

    # Auto-block: a cycle that slips/fails CYCLE_MISS_LIMIT times without a held fill in
    # between is blocked for CYCLE_BLOCK_HOURS (e.g. ULTIMA/USDC that never fills).
    cycle_miss_limit: int = 3
    cycle_block_hours: float = 4.0

    # Sync checks: reject feeds lagging the exchange clock; legs further apart in time
    # than MAX_LEG_SKEW_MS, or edges above REST_VERIFY_BPS, are re-checked via REST.
    max_feed_lag_ms: float = 3000.0
    max_leg_skew_ms: float = 2000.0
    rest_verify_enabled: bool = True
    rest_verify_bps: float = 100.0
    # WS vs REST best price divergence (bps) that invalidates the websocket book
    rest_divergence_bps: float = 20.0

    daily_loss_limit: float = 10.0
    max_concurrent: int = 1

    taker_fee_override: str = ""
    # Per-pair fees: "mexc:SUI/USDC=0,kucoin:TEL/USDC=0.001"
    taker_fee_pair_override: str = ""
    # Optional read-only keys for account fee tiers (JSON):
    # {"mexc":{"apiKey":"..","secret":".."},"kucoin":{"apiKey":"..","secret":"..","password":".."}}
    exchange_api_keys: str = ""
    paper_balances: str = '{"mexc":{"USDT":1000},"kucoin":{"USDT":1000}}'

    telegram_bot_token: str = ""
    telegram_chat_ids: str = ""

    opportunities_log: str = "logs/opportunities.jsonl"
    # All-time PnL is rebuilt from the log at startup; optionally only from this local
    # date/time on (e.g. "2026-09-27" or "2026-09-27 02:50"). Empty = whole log.
    pnl_history_since: str = ""
    # Log the same cycle's pre-check reject at most once per this many seconds
    reject_log_seconds: float = 30.0
    log_reject_product_bps: float = 20.0

    mexc_ws_url: str = "wss://wbs-api.mexc.com/ws"
    mexc_ws_legacy_url: str = "wss://wbs.mexc.com/ws"
    mexc_subs_per_conn: int = 30
    kucoin_symbols_per_topic: int = 100

    @field_validator("trading_mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        value = value.lower().strip()
        if value not in {"paper", "live"}:
            raise ValueError("trading_mode must be paper or live")
        return value

    @property
    def exchange_list(self) -> list[str]:
        return [item.strip().lower() for item in self.exchanges.split(",") if item.strip()]

    @property
    def start_asset_list(self) -> list[str]:
        return [item.strip().upper() for item in self.start_assets.split(",") if item.strip()]

    @property
    def chat_id_list(self) -> list[int]:
        return [int(item.strip()) for item in self.telegram_chat_ids.split(",") if item.strip()]

    @property
    def fee_overrides(self) -> dict[str, float]:
        if not self.taker_fee_override.strip():
            return {}
        out: dict[str, float] = {}
        for part in self.taker_fee_override.split(","):
            if ":" not in part:
                continue
            name, fee = part.split(":", 1)
            out[name.strip().lower()] = float(fee.strip())
        return out

    @property
    def pnl_history_since_ts(self) -> float | None:
        from datetime import datetime

        value = self.pnl_history_since.strip()
        if not value:
            return None
        return datetime.fromisoformat(value).timestamp()

    def min_bps_for(self, exchange: str) -> float:
        for part in self.min_net_profit_bps_by_exchange.split(","):
            if ":" not in part:
                continue
            name, bps = part.split(":", 1)
            if name.strip().lower() == exchange.lower():
                return float(bps.strip())
        return self.min_net_profit_bps

    @property
    def pair_fee_overrides(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for part in self.taker_fee_pair_override.split(","):
            part = part.strip()
            if ":" not in part or "=" not in part:
                continue
            exchange, rest = part.split(":", 1)
            symbol, fee = rest.rsplit("=", 1)
            out.setdefault(exchange.strip().lower(), {})[symbol.strip().upper()] = float(fee.strip())
        return out

    def api_keys_map(self) -> dict[str, dict[str, str]]:
        import json

        if not self.exchange_api_keys.strip():
            return {}
        raw: Any = json.loads(self.exchange_api_keys)
        return {str(ex).lower(): {str(k): str(v) for k, v in creds.items()} for ex, creds in raw.items()}

    def paper_balances_map(self) -> dict[str, dict[str, float]]:
        import json

        raw: Any = json.loads(self.paper_balances)
        return {
            str(ex).lower(): {str(asset).upper(): float(amount) for asset, amount in balances.items()}
            for ex, balances in raw.items()
        }

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode == "live" and self.enable_live


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
