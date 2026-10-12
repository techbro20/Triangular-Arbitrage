from triangular_arbitrage.config import Settings
from triangular_arbitrage.inventory import Inventory
from triangular_arbitrage.models import HopFill, RejectReason
from triangular_arbitrage.risk import RiskManager


def test_inventory_apply_hops():
    inv = Inventory({"USDT": 100.0})
    hops = (
        HopFill("BTC/USDT", "buy", "USDT", "BTC", 50.0, 0.5, 100.0),
        HopFill("ETH/BTC", "buy", "BTC", "ETH", 0.5, 10.0, 0.05),
        HopFill("ETH/USDT", "sell", "ETH", "USDT", 10.0, 55.0, 5.5),
    )
    inv.apply_hops(hops)
    assert abs(inv.get("USDT") - 105.0) < 1e-9
    assert abs(inv.get("BTC")) < 1e-9
    assert abs(inv.get("ETH")) < 1e-9


def test_risk_kill_and_pause():
    settings = Settings(daily_loss_limit=10, max_notional=50, max_concurrent=1)
    risk = RiskManager(settings)
    assert risk.allow("mexc", 10) is None
    risk.pause("mexc")
    assert risk.allow("mexc", 10) == RejectReason.PAUSED
    risk.resume("mexc")
    assert risk.allow("mexc", 10) is None
    risk.kill()
    assert risk.allow("mexc", 10) == RejectReason.KILLED


def test_risk_daily_loss_trips_pause():
    settings = Settings(daily_loss_limit=5, max_notional=50, max_concurrent=1)
    risk = RiskManager(settings)
    risk.record_pnl("mexc", -5.0)
    assert risk.is_paused("mexc")
    assert risk.allow("mexc", 10) == RejectReason.PAUSED


def test_risk_pnl_never_resets_in_session():
    settings = Settings(daily_loss_limit=100, max_notional=50, max_concurrent=1)
    risk = RiskManager(settings)
    risk.record_pnl("kucoin", 0.5095)
    risk.record_pnl("mexc", 0.1874)
    risk.record_pnl("kucoin", 0.0856)
    assert abs(risk.session_pnl("kucoin") - 0.5951) < 1e-9
    assert abs(risk.session_pnl("mexc") - 0.1874) < 1e-9
    assert abs(risk.total_session_pnl() - 0.7825) < 1e-9


def test_risk_rejects_oversize():
    settings = Settings(daily_loss_limit=10, max_notional=20, max_concurrent=1)
    risk = RiskManager(settings)
    assert risk.allow("kucoin", 25) == RejectReason.RISK_LIMIT


def test_risk_in_flight_is_per_exchange():
    settings = Settings(daily_loss_limit=10, max_notional=50, max_concurrent=1)
    risk = RiskManager(settings)
    risk.acquire("mexc")
    assert risk.allow("mexc", 10) == RejectReason.RISK_LIMIT
    assert risk.allow("kucoin", 10) is None
    risk.release("mexc")
    assert risk.allow("mexc", 10) is None
