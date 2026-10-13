import json
import time

from triangular_arbitrage.history import load_history
from triangular_arbitrage.trades import TradeBook


def _accepted(ts, exchange, cycle, profit):
    return {
        "ts": ts,
        "exchange": exchange,
        "status": "accepted",
        "cycle": cycle,
        "start_asset": "USDT",
        "start_amount": 50.0,
        "end_amount": 50.0 + profit,
        "net_profit": profit,
        "net_profit_bps": profit / 50.0 * 10_000,
        "theoretical_product": 1.003,
        "legs": [
            {"market": "LINK/USDT", "side": "buy"},
            {"market": "LINK/USDC", "side": "sell"},
            {"market": "USDC/USDT", "side": "sell"},
        ],
    }


def _miss(ts, exchange, cycle, status, profit=None):
    return {
        "ts": ts,
        "exchange": exchange,
        "status": status,
        "cycle": cycle,
        "expected_bps": 40.0,
        "realised_bps": None,
        "realised_profit": profit,
    }


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.write('{"ts": 1, "exchange": "bingx", "status": "accep')  # torn last line


def test_replay_restores_pnl_trades_and_blocks(tmp_path):
    now = time.time()
    log = tmp_path / "opp.jsonl"
    _write(
        log,
        [
            _accepted(now - 500, "bingx", "link", 0.13),
            {"ts": now - 450, "exchange": "bingx", "status": "rejected", "reason": "slippage", "cycle": "x"},
            _accepted(now - 400, "bingx", "uni", 0.27),
            _miss(now - 300, "mexc", "fet", "slipped", -0.50),
            _accepted(now - 250, "mexc", "qnt", 0.30),
            _miss(now - 200, "bingx", "ultima", "failed"),
            _miss(now - 150, "bingx", "ultima", "failed"),
            _miss(now - 100, "bingx", "ultima", "failed"),
        ],
    )
    history, recent = load_history(str(log), miss_limit=3, block_hours=4)

    bingx = history.get("bingx")
    mexc = history.get("mexc")
    assert bingx.booked_trades == 2 and abs(bingx.booked_pnl - 0.40) < 1e-9
    assert bingx.failed == 3
    assert mexc.booked_trades == 1 and mexc.slipped == 1
    assert abs(mexc.honest_pnl - (0.30 - 0.50)) < 1e-9
    total, count = history.total_booked()
    assert count == 3 and abs(total - 0.70) < 1e-9

    assert "ultima" in history.active_blocks("bingx")
    assert len(recent) == 3 and recent[-1].exchange == "mexc"
    assert recent[0].path == "B LINK/USDT → S LINK/USDC → S USDC/USDT"

    book = TradeBook()
    book.seed(recent)
    assert book.recent(1)[0].net_profit == 0.30
    assert book.total_count() == 0  # session counts start fresh


def test_since_filter_and_held_resets_streak(tmp_path):
    now = time.time()
    log = tmp_path / "opp.jsonl"
    _write(
        log,
        [
            _accepted(now - 10_000, "bingx", "old", 1.0),
            _miss(now - 300, "bingx", "link", "failed"),
            _miss(now - 200, "bingx", "link", "failed"),
            _accepted(now - 150, "bingx", "link", 0.1),
            _miss(now - 100, "bingx", "link", "failed"),
        ],
    )
    history, _ = load_history(str(log), miss_limit=3, block_hours=4, since_ts=now - 1000)
    assert history.get("bingx").booked_trades == 1
    assert not history.active_blocks("bingx")
    assert history.streaks["bingx"]["link"] == 1


def _rest_mismatch(ts, exchange, cycle, detail="ws 30.0bps, rest fees"):
    return {
        "ts": ts,
        "exchange": exchange,
        "status": "rejected",
        "cycle": cycle,
        "reason": "rest_mismatch",
        "detail": detail,
        "theoretical_product": 1.003,
    }


def test_rest_mismatches_count_toward_block_but_rest_outages_do_not(tmp_path):
    now = time.time()
    log = tmp_path / "opp.jsonl"
    _write(
        log,
        [
            _rest_mismatch(now - 300, "bingx", "ultima"),
            _rest_mismatch(now - 250, "bingx", "ultima", "rest not ready"),
            _rest_mismatch(now - 200, "bingx", "ultima", "rest error TimeoutError()"),
            _rest_mismatch(now - 150, "bingx", "ultima"),
            _miss(now - 100, "bingx", "ultima", "failed"),
        ],
    )
    history, _ = load_history(str(log), miss_limit=3, block_hours=4)
    assert history.get("bingx").rest_mismatch == 2
    assert "ultima" in history.active_blocks("bingx")


def test_blocks_escalate_and_reset_after_held(tmp_path):
    from triangular_arbitrage.history import PnlHistory

    h = PnlHistory()
    assert h.block("bingx", "c", 0.0, 4, 168) == (4 * 3600.0, 4)
    assert h.block("bingx", "c", 0.0, 4, 168)[1] == 8
    assert h.block("bingx", "c", 0.0, 4, 168)[1] == 16
    for _ in range(4):
        h.block("bingx", "c", 0.0, 4, 168)
    assert h.block("bingx", "c", 0.0, 4, 168)[1] == 168  # capped
    h.record_booked("bingx", "c", 0.1)
    assert h.block("bingx", "c", 0.0, 4, 168)[1] == 4


def test_replay_escalates_repeat_offender(tmp_path):
    # ULTIMA-like: blocked, comes back after the block, fails again -> next block is 8h
    day = 86_400.0
    now = time.time()
    t0 = now - 2 * day
    log = tmp_path / "opp.jsonl"
    _write(
        log,
        [
            _miss(t0, "bingx", "ultima", "failed"),
            _miss(t0 + 60, "bingx", "ultima", "failed"),
            _miss(t0 + 120, "bingx", "ultima", "failed"),  # block 1: 4h
            _rest_mismatch(now - 600, "bingx", "ultima"),
            _rest_mismatch(now - 300, "bingx", "ultima"),
            _miss(now - 60, "bingx", "ultima", "failed"),  # block 2: 8h
        ],
    )
    history, _ = load_history(str(log), miss_limit=3, block_hours=4, block_max_hours=168)
    until = history.active_blocks("bingx")["ultima"]
    assert abs(until - (now - 60 + 8 * 3600)) < 1


def test_missing_log_is_empty(tmp_path):
    history, recent = load_history(str(tmp_path / "nope.jsonl"), miss_limit=3, block_hours=4)
    assert history.total_booked() == (0.0, 0) and recent == []
