import asyncio
from types import SimpleNamespace

from ccxt.base.errors import RequestTimeout

from triangular_arbitrage import worker
from triangular_arbitrage.exchange import ccxt_rest


class _Alerts:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _ctx(stop: asyncio.Event) -> SimpleNamespace:
    return SimpleNamespace(
        stop=stop,
        risk=SimpleNamespace(killed=False),
        statuses={},
        alerts=_Alerts(),
    )


def _run(monkeypatch, failures: list[BaseException]):
    sleeps: list[float] = []

    async def run():
        stop = asyncio.Event()
        ctx = _ctx(stop)
        remaining = list(failures)

        async def fake_session(exchange, ctx_):
            if remaining:
                raise remaining.pop(0)
            stop.set()

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(worker, "_run_session", fake_session)
        monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
        await worker.run_worker("mexc", ctx)
        return ctx

    ctx = asyncio.run(run())
    return ctx, sleeps


def test_network_errors_back_off_and_alert_once(monkeypatch):
    ctx, sleeps = _run(monkeypatch, [RequestTimeout("t"), OSError("dns"), RequestTimeout("t")])
    assert sleeps == [5.0, 10.0, 20.0]
    assert len(ctx.alerts.messages) == 1
    assert "network outage" in ctx.alerts.messages[0]
    assert ctx.statuses["mexc"].errors == 3


def test_backoff_caps_at_max(monkeypatch):
    _, sleeps = _run(monkeypatch, [RequestTimeout("t")] * 8)
    assert max(sleeps) == worker._OUTAGE_BACKOFF_MAX


def test_other_errors_keep_fixed_retry(monkeypatch):
    ctx, sleeps = _run(monkeypatch, [ValueError("bug")])
    assert sleeps == [5]
    assert "worker error" in ctx.alerts.messages[0]


class _SpotClient:
    id = "mexc"

    def __init__(self) -> None:
        self.markets = None
        self.load_markets_called = False

    async def fetch_spot_markets(self, params):
        return [{"symbol": "BTC/USDT"}]

    async def load_markets(self, reload=False):
        self.load_markets_called = True
        return {}

    def set_markets(self, markets):
        self.markets = markets


def test_spot_markets_never_use_load_markets():
    client = _SpotClient()
    raw = asyncio.run(ccxt_rest._fetch_raw_markets(client))
    assert raw == [{"symbol": "BTC/USDT"}]
    asyncio.run(ccxt_rest._load_spot_into(client))
    assert client.markets == [{"symbol": "BTC/USDT"}]
    assert not client.load_markets_called
