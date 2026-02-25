from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from sortedcontainers import SortedDict


class OrderStatus(Enum):
    NEW = auto()
    PARTIAL = auto()
    FILLED = auto()
    CANCELLED = auto()


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str            # "BUY" | "SELL"
    order_type: str      # "LIMIT" | "MARKET"
    price: int           # int64 ticks; 0 for MARKET orders
    qty: int             # original quantity
    leaves_qty: int      # remaining open quantity
    cum_qty: int = 0
    cum_value_ticks: int = 0  # running sum of fill_price * fill_qty for VWAP
    status: OrderStatus = OrderStatus.NEW

    def apply_fill(self, fill_qty: int, fill_price: int) -> None:
        assert self.status in (OrderStatus.NEW, OrderStatus.PARTIAL), (
            f"Cannot fill order {self.order_id} in status {self.status}"
        )
        assert 0 < fill_qty <= self.leaves_qty, (
            f"Invalid fill_qty {fill_qty} for leaves_qty {self.leaves_qty}"
        )
        self.leaves_qty -= fill_qty
        self.cum_qty += fill_qty
        self.cum_value_ticks += fill_qty * fill_price
        self.status = OrderStatus.FILLED if self.leaves_qty == 0 else OrderStatus.PARTIAL

    def apply_cancel(self) -> None:
        assert self.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED), (
            f"Cannot cancel order {self.order_id} in status {self.status}"
        )
        self.status = OrderStatus.CANCELLED

    @property
    def avg_px(self) -> int:
        return self.cum_value_ticks // self.cum_qty if self.cum_qty > 0 else 0


class LimitOrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bids: SortedDict = SortedDict(lambda x: -x)  # descending: best bid at index 0
        self._asks: SortedDict = SortedDict()               # ascending: best ask at index 0
        self._order_map: dict[str, Order] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def submit(self, order: Order) -> list[tuple]:
        assert order.symbol == self.symbol, (
            f"Order symbol {order.symbol} does not match LOB symbol {self.symbol}"
        )
        if order.order_type == "LIMIT":
            assert order.price > 0, f"LIMIT order must have price > 0, got {order.price}"
            return self._add_limit_order(order)
        elif order.order_type == "MARKET":
            assert order.price == 0, f"MARKET order must have price == 0, got {order.price}"
            return self._add_market_order(order)
        else:
            raise ValueError(f"Unknown order_type: {order.order_type}")

    def cancel(self, order_id: str) -> None:
        order = self._order_map.get(order_id)
        if order is None:
            return
        order.apply_cancel()

    def best_bid(self) -> Optional[int]:
        return self._bids.peekitem(0)[0] if self._bids else None

    def best_ask(self) -> Optional[int]:
        return self._asks.peekitem(0)[0] if self._asks else None

    def spread(self) -> Optional[int]:
        bb = self.best_bid()
        ba = self.best_ask()
        return ba - bb if (bb is not None and ba is not None) else None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _add_limit_order(self, order: Order) -> list[tuple]:
        self._order_map[order.order_id] = order
        fills: list[tuple] = []

        if order.side == "BUY":
            while self._asks and order.leaves_qty > 0:
                best_ask_price, level = self._asks.peekitem(0)
                if order.price < best_ask_price:
                    break
                fills += self._fill_from_level(level, order, best_ask_price)
                if not level:
                    del self._asks[best_ask_price]
        else:  # SELL
            while self._bids and order.leaves_qty > 0:
                best_bid_price, level = self._bids.peekitem(0)
                if order.price > best_bid_price:
                    break
                fills += self._fill_from_level(level, order, best_bid_price)
                if not level:
                    del self._bids[best_bid_price]

        # Rest unfilled remainder in book
        if order.leaves_qty > 0 and order.status != OrderStatus.CANCELLED:
            target = self._bids if order.side == "BUY" else self._asks
            target.setdefault(order.price, deque()).append(order)

        return fills

    def _add_market_order(self, order: Order) -> list[tuple]:
        self._order_map[order.order_id] = order
        fills: list[tuple] = []

        # BUY sweeps asks (ascending), SELL sweeps bids (descending key = best first)
        side = self._asks if order.side == "BUY" else self._bids
        prices_to_prune: list = []

        for price, level in side.items():
            if order.leaves_qty <= 0:
                break
            fills += self._fill_from_level(level, order, price)
            if not level:
                prices_to_prune.append(price)

        for p in prices_to_prune:
            del side[p]

        # Market orders do NOT rest in book
        return fills

    def _fill_from_level(
        self, level: deque, incoming: Order, fill_price: int
    ) -> list[tuple]:
        fills: list[tuple] = []
        while level and incoming.leaves_qty > 0:
            resting = level[0]
            if resting.status == OrderStatus.CANCELLED:
                level.popleft()
                continue
            fill_qty = min(resting.leaves_qty, incoming.leaves_qty)
            resting.apply_fill(fill_qty, fill_price)
            incoming.apply_fill(fill_qty, fill_price)
            fills.append((resting.order_id, incoming.order_id, fill_qty, fill_price))
            if resting.leaves_qty == 0:
                level.popleft()
        return fills
