from triangular_arbitrage.exchange.mexc_proto import encode_varint, parse_mexc_limit_depth


def _len_delim(field: int, payload: bytes) -> bytes:
    return encode_varint((field << 3) | 2) + encode_varint(len(payload)) + payload


def test_parse_mexc_limit_depth_roundtrip():
    # PublicLimitDepthV3ApiItem: price=1, quantity=2
    bid_item = _len_delim(1, b"100.5") + _len_delim(2, b"2.0")
    ask_item = _len_delim(1, b"101.0") + _len_delim(2, b"1.5")
    depth = _len_delim(2, bid_item) + _len_delim(1, ask_item)
    wrapper = _len_delim(1, b"spot@public.limit.depth.v3.api.pb@BTCUSDT@5")
    wrapper += _len_delim(3, b"BTCUSDT")
    wrapper += _len_delim(303, depth)

    symbol, bids, asks = parse_mexc_limit_depth(wrapper)
    assert symbol == "BTCUSDT"
    assert bids == [(100.5, 2.0)]
    assert asks == [(101.0, 1.5)]
