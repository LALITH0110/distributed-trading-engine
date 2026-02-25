"""
Comprehensive pytest suite for LimitOrderBook.

Requirement coverage:
  ME-01 — Price-time priority (FIFO within level) + bid/ask ordering
  ME-02 — Limit rests, market sweeps, cancel (FIX semantics)
  ME-04 — State machine transitions and violation assertions
  ME-05 — Lazy cancel O(1): cancelled order skipped during fill
  ME-06 — Ghost price-level pruning after full drain
  ME-08 — int64 tick prices throughout; fill_price is int
  ME-10 — Crossed-book, locked-book, partial-cross-then-rest
"""

from __future__ import annotations

import pytest

from engine.lob import LimitOrderBook, Order, OrderStatus


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------

def make_order(order_id: str, side: str, order_type: str, price: int, qty: int,
               symbol: str = "BTC") -> Order:
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=price,
        qty=qty,
        leaves_qty=qty,
    )


# ---------------------------------------------------------------------------
# ME-01 — Price-time priority (FIFO within price level)
# ---------------------------------------------------------------------------

class TestPriceTimePriority:
    """ME-01: first order at a price level fills before later orders at same price."""

    def test_fifo_same_price_first_submitted_fills_first(self):
        """Two sells at same price — first submitted fills first."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 50)
        s2 = make_order("s2", "SELL", "LIMIT", 10000, 50)
        lob.submit(s1)
        lob.submit(s2)

        b1 = make_order("b1", "BUY", "MARKET", 0, 50)
        fills = lob.submit(b1)

        assert len(fills) == 1
        filled_resting_id = fills[0][0]
        assert filled_resting_id == "s1", "s1 should fill first (price-time priority)"
        assert s1.status == OrderStatus.FILLED
        assert s2.status == OrderStatus.NEW  # untouched

    def test_ask_side_ascending_order(self):
        """Best ask is the lowest ask price. ME-01."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("s1", "SELL", "LIMIT", 10200, 10))
        lob.submit(make_order("s2", "SELL", "LIMIT", 10100, 10))
        lob.submit(make_order("s3", "SELL", "LIMIT", 10300, 10))

        assert lob.best_ask() == 10100

    def test_bid_side_descending_order(self):
        """Best bid is the highest bid price. ME-01."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("b1", "BUY", "LIMIT", 9900, 10))
        lob.submit(make_order("b2", "BUY", "LIMIT", 10000, 10))
        lob.submit(make_order("b3", "BUY", "LIMIT", 9800, 10))

        assert lob.best_bid() == 10000


# ---------------------------------------------------------------------------
# ME-02 — Limit rests, market sweeps, cancel
# ---------------------------------------------------------------------------

class TestLimitAndMarket:
    """ME-02: limit orders rest when no cross; market orders sweep fully."""

    def test_limit_rests_no_cross(self):
        """Limit buy with no asks: rests in book, 0 fills. ME-02."""
        lob = LimitOrderBook("BTC")
        b1 = make_order("b1", "BUY", "LIMIT", 9900, 100)
        fills = lob.submit(b1)

        assert fills == []
        assert lob.best_bid() == 9900
        assert lob.best_ask() is None

    def test_market_buy_sweeps_fully(self):
        """Market buy consumes exactly matching sell. ME-02."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        fills = lob.submit(b1)

        assert len(fills) == 1
        assert fills[0][2] == 100   # fill_qty
        assert fills[0][3] == 10000  # fill_price
        assert s1.status == OrderStatus.FILLED
        assert lob.best_ask() is None
        assert lob.best_bid() is None

    def test_cancel_order_skips_in_fill(self):
        """Cancelled sell is skipped; crossing buy gets 0 fills. ME-02."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)
        lob.cancel("s1")

        assert s1.status == OrderStatus.CANCELLED

        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        fills = lob.submit(b1)
        assert fills == []

    def test_market_against_empty_book_returns_empty(self):
        """Market buy on empty book: [] fills, no exception. ME-02."""
        lob = LimitOrderBook("BTC")
        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        fills = lob.submit(b1)
        assert fills == []


# ---------------------------------------------------------------------------
# ME-04 — Order state machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    """ME-04: legal transitions and assertion guards for illegal ones."""

    def test_new_to_partial(self):
        """Partial fill: NEW -> PARTIAL. ME-04."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "MARKET", 0, 60)
        lob.submit(b1)

        assert s1.status == OrderStatus.PARTIAL
        assert s1.leaves_qty == 40

    def test_new_to_filled_single_fill(self):
        """Full fill in one shot: NEW -> FILLED. ME-04."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        lob.submit(b1)

        assert s1.status == OrderStatus.FILLED

    def test_partial_to_filled(self):
        """Two partial fills complete the order: PARTIAL -> FILLED. ME-04."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "MARKET", 0, 40)
        lob.submit(b1)
        assert s1.status == OrderStatus.PARTIAL

        b2 = make_order("b2", "BUY", "MARKET", 0, 60)
        lob.submit(b2)
        assert s1.status == OrderStatus.FILLED

    def test_double_fill_raises(self):
        """apply_fill on already-FILLED order raises AssertionError. ME-04."""
        order = make_order("x1", "SELL", "LIMIT", 10000, 100)
        order.apply_fill(100, 10000)  # now FILLED
        with pytest.raises(AssertionError):
            order.apply_fill(1, 10000)  # must raise

    def test_cancel_of_filled_raises(self):
        """apply_cancel on FILLED order raises AssertionError. ME-04."""
        order = make_order("x1", "SELL", "LIMIT", 10000, 100)
        order.apply_fill(100, 10000)
        with pytest.raises(AssertionError):
            order.apply_cancel()

    def test_cancel_of_cancelled_raises(self):
        """apply_cancel on already-CANCELLED raises AssertionError. ME-04."""
        order = make_order("x1", "SELL", "LIMIT", 10000, 100)
        order.apply_cancel()
        with pytest.raises(AssertionError):
            order.apply_cancel()


# ---------------------------------------------------------------------------
# ME-05 — Lazy cancel O(1)
# ---------------------------------------------------------------------------

class TestLazyCancel:
    """ME-05: O(1) cancel; cancelled order evicted lazily during fill."""

    def test_lazy_cancel_skips_first_order_fills_second(self):
        """Cancel s1; crossing buy fills s2 instead. s1 never participates. ME-05."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        s2 = make_order("s2", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)
        lob.submit(s2)

        lob.cancel("s1")
        assert s1.status == OrderStatus.CANCELLED

        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        fills = lob.submit(b1)

        # s1 skipped (lazy), s2 fills
        assert len(fills) == 1
        assert fills[0][0] == "s2"
        assert s2.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# ME-06 — Ghost level pruning
# ---------------------------------------------------------------------------

class TestGhostLevelPruning:
    """ME-06: price key removed from SortedDict after level fully drained."""

    def test_ask_level_pruned_after_full_drain(self):
        """After market buy fully drains ask level, key removed from _asks. ME-06."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        lob.submit(b1)

        assert 10000 not in lob._asks
        assert lob.best_ask() is None

    def test_bid_level_pruned_after_full_drain(self):
        """After market sell fully drains bid level, key removed from _bids. ME-06."""
        lob = LimitOrderBook("BTC")
        b1 = make_order("b1", "BUY", "LIMIT", 9900, 100)
        lob.submit(b1)

        s1 = make_order("s1", "SELL", "MARKET", 0, 100)
        lob.submit(s1)

        assert 9900 not in lob._bids
        assert lob.best_bid() is None


# ---------------------------------------------------------------------------
# ME-08 — int64 tick prices throughout
# ---------------------------------------------------------------------------

class TestIntTickPrices:
    """ME-08: all prices are int ticks, including fill_price in fill tuples."""

    @pytest.mark.parametrize("price,qty", [
        (10000, 100),  # $100.00 in ticks
        (10050, 50),   # $100.50 in ticks
        (99999, 1),    # large tick value
    ])
    def test_fill_price_is_int(self, price, qty):
        """fill_price in fill tuple must be int (not float). ME-08."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("s1", "SELL", "LIMIT", price, qty))
        b1 = make_order("b1", "BUY", "MARKET", 0, qty)
        fills = lob.submit(b1)

        assert len(fills) == 1
        fill_price = fills[0][3]
        assert isinstance(fill_price, int), f"fill_price must be int, got {type(fill_price)}"


# ---------------------------------------------------------------------------
# ME-10 — Crossed-book prevention
# ---------------------------------------------------------------------------

class TestCrossedBook:
    """ME-10: limit order at or above best ask triggers immediate fill."""

    def test_limit_buy_at_ask_fills_immediately(self):
        """Limit buy at exact ask price fills immediately (not resting). ME-10."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "LIMIT", 10000, 100)
        fills = lob.submit(b1)

        assert len(fills) == 1
        assert fills[0][2] == 100
        assert lob.best_ask() is None
        assert lob.best_bid() is None

    def test_locked_book_bid_equals_ask_triggers_fill(self):
        """bid == ask is crossing condition (== not just >). ME-10."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("s1", "SELL", "LIMIT", 10000, 50))

        b1 = make_order("b1", "BUY", "LIMIT", 10000, 50)
        fills = lob.submit(b1)

        assert len(fills) == 1
        assert fills[0][3] == 10000

    def test_partial_cross_then_rest(self):
        """Limit buy crosses partially; remainder rests at bid. ME-10."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 50)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "LIMIT", 10000, 100)
        fills = lob.submit(b1)

        assert len(fills) == 1
        assert fills[0][2] == 50   # 50 filled
        assert b1.leaves_qty == 50  # 50 remains
        assert lob.best_bid() == 10000  # remainder rests
        assert lob.best_ask() is None


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Canonical identity, VWAP, and boundary behaviors."""

    def test_partial_fill_mutates_canonical_object(self):
        """Partial fill mutates Order in-place; order_map holds same object. ME-04/ME-09."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "LIMIT", 10000, 100)
        lob.submit(s1)

        b1 = make_order("b1", "BUY", "MARKET", 0, 60)
        lob.submit(b1)

        assert s1.leaves_qty == 40
        assert lob._order_map["s1"] is s1  # same object, not a copy

    def test_vwap_avg_px_multi_level(self):
        """VWAP computed as cum_value_ticks // cum_qty across two price levels. ME-08."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("s1", "SELL", "LIMIT", 10000, 50))
        lob.submit(make_order("s2", "SELL", "LIMIT", 10200, 50))

        b1 = make_order("b1", "BUY", "MARKET", 0, 100)
        lob.submit(b1)

        expected_avg = (50 * 10000 + 50 * 10200) // 100  # == 10100
        assert b1.avg_px == expected_avg

    def test_two_different_orders_opposite_sides_fill_normally(self):
        """Two distinct order_ids on opposite sides fill without self-trade issues."""
        lob = LimitOrderBook("BTC")
        seller = make_order("seller-1", "SELL", "LIMIT", 10000, 100)
        buyer = make_order("buyer-1", "BUY", "LIMIT", 10000, 100)
        lob.submit(seller)
        fills = lob.submit(buyer)

        assert len(fills) == 1
        assert fills[0][0] == "seller-1"
        assert fills[0][1] == "buyer-1"
        assert seller.status == OrderStatus.FILLED
        assert buyer.status == OrderStatus.FILLED

    def test_spread_computed_correctly(self):
        """spread() = best_ask - best_bid when both sides have orders."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("b1", "BUY", "LIMIT", 9900, 10))
        lob.submit(make_order("s1", "SELL", "LIMIT", 10100, 10))
        assert lob.spread() == 200

    def test_spread_none_when_one_side_empty(self):
        """spread() returns None when one side has no orders."""
        lob = LimitOrderBook("BTC")
        lob.submit(make_order("b1", "BUY", "LIMIT", 9900, 10))
        assert lob.spread() is None

    def test_market_sell_against_empty_book_returns_empty(self):
        """Market sell on empty book: [] fills, no exception."""
        lob = LimitOrderBook("BTC")
        s1 = make_order("s1", "SELL", "MARKET", 0, 100)
        fills = lob.submit(s1)
        assert fills == []
