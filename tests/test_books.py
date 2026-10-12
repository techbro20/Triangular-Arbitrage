from triangular_arbitrage.books import BookStore, LocalOrderBook


def test_invalidate_clears_book():
    book = LocalOrderBook()
    book.apply_snapshot([(100.0, 1.0)], [(101.0, 1.0)])
    assert book.ready
    book.invalidate()
    assert not book.ready
    assert book.age_seconds() == float("inf")


def test_bookstore_invalidate_many():
    store = BookStore()
    store.apply_snapshot("BTC/USDT", [(1.0, 1.0)], [(2.0, 1.0)])
    store.apply_snapshot("ETH/USDT", [(1.0, 1.0)], [(2.0, 1.0)])
    assert store.ready_count() == 2
    store.invalidate_many(["BTC/USDT", "ETH/USDT"])
    assert store.ready_count() == 0
