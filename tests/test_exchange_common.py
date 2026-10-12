from triangular_arbitrage.exchange.common import chunks, levels_from_rows


def test_chunks():
    assert chunks(["a", "b", "c", "d"], 2) == [["a", "b"], ["c", "d"]]


def test_levels_from_rows():
    rows = [["1.5", "2"], {"price": "3", "quantity": "4"}, ["0", "1"]]
    levels = levels_from_rows(rows)
    assert (1.5, 2.0) in levels
    assert (3.0, 4.0) in levels
