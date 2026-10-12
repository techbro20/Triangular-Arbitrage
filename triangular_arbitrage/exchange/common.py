from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class FeedStats:
    reconnects: int = 0
    parse_errors: int = 0
    snapshots: int = 0
    mode: str = "ws"
    last_error: str = ""
    last_message_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "reconnects": self.reconnects,
            "parse_errors": self.parse_errors,
            "snapshots": self.snapshots,
            "mode": self.mode,
            "last_error": self.last_error[:120],
            "last_message_age_s": round(time.time() - self.last_message_ts, 1)
            if self.last_message_ts
            else None,
        }


def chunks(items: Sequence[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def levels_from_rows(rows) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows or []:
        if isinstance(row, dict):
            price = row.get("p") or row.get("price") or row.get(0)
            qty = row.get("v") or row.get("quantity") or row.get("amount") or row.get(1)
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, qty = row[0], row[1]
        else:
            continue
        try:
            p, q = float(price), float(qty)
        except (TypeError, ValueError):
            continue
        if q > 0:
            out.append((p, q))
    return out


def exchange_ts_seconds(value) -> float | None:
    """Exchange timestamp (ms, or s) → epoch seconds; None if missing or implausible."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e14:  # microseconds
        ts /= 1_000_000.0
    elif ts > 1e11:  # milliseconds
        ts /= 1000.0
    # 2020-01-01 .. 2100-01-01
    if not 1_577_836_800 <= ts <= 4_102_444_800:
        return None
    return ts


def as_version(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


BACKOFF_START = 1.0
BACKOFF_MAX = 60.0
# Quiet books can sit idle; keepalive pings should arrive sooner than this.
RECV_TIMEOUT = 120.0
