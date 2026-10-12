from triangular_arbitrage.exchange.bingx import BingxAdapter
from triangular_arbitrage.exchange.bybit import BybitAdapter
from triangular_arbitrage.exchange.gateio import GateioAdapter
from triangular_arbitrage.exchange.htx import HtxAdapter
from triangular_arbitrage.exchange.kucoin import KucoinAdapter
from triangular_arbitrage.exchange.mexc import MexcAdapter

ADAPTERS = {
    "mexc": MexcAdapter,
    "kucoin": KucoinAdapter,
    "gateio": GateioAdapter,
    "htx": HtxAdapter,
    "bingx": BingxAdapter,
    "bybit": BybitAdapter,
}
