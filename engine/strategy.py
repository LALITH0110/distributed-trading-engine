"""
engine/strategy.py — BaseStrategy ABC + process runner for strategy nodes.

Subclass BaseStrategy and implement on_tick / on_fill.  Call start_strategy()
to spawn the node as a daemon Process.

Context-after-fork
------------------
All zmq imports and Context creation happen inside _strategy_target — after
multiprocessing forks.  No zmq at module level.

Safe-mode
---------
If no Heartbeat is received from the feed handler within heartbeat_timeout_s,
the strategy enters safe mode: ticks are suppressed and all open orders are
cancelled via CancelRequest sent over order_push (which the RG forwards to
the ME via the cancel_push path).  Safe mode auto-recovers when the next
Heartbeat arrives.
"""

from __future__ import annotations

import abc
import multiprocessing
from collections import deque

import numpy as np


# ── Public ABC ────────────────────────────────────────────────────────────────

class BaseStrategy(abc.ABC):
    """Abstract base for all strategy nodes.

    Subclassers implement:
        on_tick(tick: MarketDataTick) -> None
        on_fill(exec_report: ExecutionReport) -> None

    Optionally override:
        on_start() -> None   (called once before event loop)
        on_stop()  -> None   (called once in finally block)

    Inside the running process, submit_order() is monkey-patched onto the
    instance by _strategy_target, giving access to the order_push socket.
    """

    def __init__(self, strategy_id: str, heartbeat_timeout_s: float = 5.0):
        self.strategy_id = strategy_id
        self.heartbeat_timeout_s = heartbeat_timeout_s

    @abc.abstractmethod
    def on_tick(self, tick) -> None: ...

    @abc.abstractmethod
    def on_fill(self, exec_report) -> None: ...

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def submit_order(self, side: int, qty: int, price: int, symbol: str) -> bool:
        raise NotImplementedError("submit_order only valid inside running process")


# ── Internal process target ────────────────────────────────────────────────────

def _strategy_target(strategy: BaseStrategy) -> None:
    """multiprocessing.Process target.

    All imports here — after fork — to satisfy the Context-after-fork rule.
    All sockets created via zmq_factory (raw ctx.socket() is banned).
    """
    import signal           # noqa: PLC0415
    import threading        # noqa: PLC0415
    import time             # noqa: PLC0415
    import uuid             # noqa: PLC0415
    import zmq              # noqa: PLC0415 — post-fork

    from engine.config import load_topology   # noqa: PLC0415
    from engine import zmq_factory            # noqa: PLC0415
    from proto.messages_pb2 import (          # noqa: PLC0415
        MarketDataTick,
        ExecutionReport,
        ExecType,
        NewOrderSingle,
        OrderType,
        CancelRequest,
    )

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)

    # ── Sockets ───────────────────────────────────────────────────────────────
    # 1. Market data subscription (feed_pub port 5555)
    feed_sub = zmq_factory.make_sub(ctx, topo, topic=b"")
    feed_sub.connect(topo.get_connect_addr("feed_pub"))

    # 2. Execution reports (exec_report_pub port 5556, topic = strategy_id)
    exec_sub = zmq_factory.make_sub(ctx, topo, topic=strategy.strategy_id.encode())
    exec_sub.connect(topo.get_connect_addr("exec_report_pub"))

    # 3. Heartbeat subscription (heartbeat_pub port 5557)
    hb_sub = zmq_factory.make_sub(ctx, topo, topic=b"")
    hb_sub.connect(topo.get_connect_addr("heartbeat_pub"))

    # 4. Order submission (order_ingress port 5558)
    order_push = zmq_factory.make_push(ctx, topo)
    order_push.connect(topo.get_connect_addr("order_ingress"))

    # 5. Risk rejects (risk_reject_pub port 5561, topic = strategy_id)
    risk_sub = zmq_factory.make_sub(ctx, topo, topic=strategy.strategy_id.encode())
    risk_sub.connect(topo.get_connect_addr("risk_reject_pub"))

    zmq_factory.sleep_for_connect(topo)

    # ── State ─────────────────────────────────────────────────────────────────
    open_orders: dict[str, tuple[str, int, int]] = {}  # order_id -> (symbol, side, price)
    last_hb_s = time.monotonic()
    safe_mode = False
    _order_seq = 0  # noqa: F841 — available for subclasses via closure if needed

    # ── Monkey-patch submit_order ─────────────────────────────────────────────
    def _submit_order(side: int, qty: int, price: int, symbol: str) -> bool:
        """Send a limit order to the risk gateway.

        Self-trade check: if a resting order on the same symbol would cross
        (BUY at price >= resting SELL price, or SELL at price <= resting BUY
        price), reject locally without hitting the RG/ME.

        Returns True if the order was submitted, False if self-trade prevented.
        """
        for _oid, (sym, rest_side, rest_price) in open_orders.items():
            if sym != symbol:
                continue
            # BUY crosses a resting SELL
            if side == 1 and rest_side == 2 and price >= rest_price:
                return False
            # SELL crosses a resting BUY
            if side == 2 and rest_side == 1 and price <= rest_price:
                return False

        nos = NewOrderSingle()
        nos.schema_version = 1
        nos.order_id = str(uuid.uuid4())
        nos.symbol = symbol
        nos.side = side
        nos.order_type = OrderType.ORDER_TYPE_LIMIT
        nos.price = price
        nos.quantity = qty
        nos.strategy_id = strategy.strategy_id

        order_push.send(nos.SerializeToString())
        # Pre-register so self-trade guard can see it immediately
        open_orders[nos.order_id] = (symbol, side, price)
        return True

    strategy.submit_order = _submit_order  # type: ignore[method-assign]

    # ── SIGTERM handler ────────────────────────────────────────────────────────
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda s, f: stop.set())

    # ── Poller ────────────────────────────────────────────────────────────────
    poller = zmq.Poller()
    poller.register(feed_sub, zmq.POLLIN)
    poller.register(exec_sub, zmq.POLLIN)
    poller.register(hb_sub, zmq.POLLIN)
    poller.register(risk_sub, zmq.POLLIN)

    strategy.on_start()

    try:
        while not stop.is_set():
            socks = dict(poller.poll(timeout=50))  # 50 ms

            # ── Heartbeat timeout check ───────────────────────────────────────
            now_s = time.monotonic()
            if not safe_mode and (now_s - last_hb_s) > strategy.heartbeat_timeout_s:
                safe_mode = True
                # Cancel all open orders via CancelRequest through order_push
                for order_id in list(open_orders):
                    cr = CancelRequest()
                    cr.schema_version = 1
                    cr.order_id = order_id
                    cr.strategy_id = strategy.strategy_id
                    order_push.send(cr.SerializeToString())

            # ── Heartbeat received ────────────────────────────────────────────
            if hb_sub in socks:
                hb_sub.recv()  # single frame — no topic prefix on PUB(b"") sub
                last_hb_s = time.monotonic()
                if safe_mode:
                    safe_mode = False  # auto-recover

            # ── Market data tick ──────────────────────────────────────────────
            if feed_sub in socks and not safe_mode:
                tick = MarketDataTick()
                tick.ParseFromString(feed_sub.recv())
                strategy.on_tick(tick)

            # ── Execution reports ─────────────────────────────────────────────
            if exec_sub in socks:
                frames = exec_sub.recv_multipart()
                er = ExecutionReport()
                er.ParseFromString(frames[1])
                terminal = (
                    ExecType.EXEC_TYPE_FILL,
                    ExecType.EXEC_TYPE_CANCEL,
                    ExecType.EXEC_TYPE_REJECT,
                )
                if er.exec_type in terminal:
                    open_orders.pop(er.order_id, None)
                if not safe_mode:
                    strategy.on_fill(er)

            # ── Risk rejects ──────────────────────────────────────────────────
            if risk_sub in socks:
                frames = risk_sub.recv_multipart()
                er = ExecutionReport()
                er.ParseFromString(frames[1])
                open_orders.pop(er.order_id, None)  # clean up pre-registered order

    finally:
        feed_sub.close()
        exec_sub.close()
        hb_sub.close()
        order_push.close()
        risk_sub.close()
        ctx.term()
        strategy.on_stop()


# ── Public API ────────────────────────────────────────────────────────────────

def start_strategy(strategy: BaseStrategy) -> multiprocessing.Process:
    """Spawn a daemon Process running the given strategy and return it.

    Parameters
    ----------
    strategy : BaseStrategy subclass instance

    Returns
    -------
    multiprocessing.Process — already started, daemon=True.
    """
    p = multiprocessing.Process(
        target=_strategy_target,
        args=(strategy,),
        daemon=True,
    )
    p.start()
    return p


# ── MeanReversionStrategy ─────────────────────────────────────────────────────

class MeanReversionStrategy(BaseStrategy):
    """Rolling z-score mean-reversion strategy. STRAT-03.

    Emits BUY when z < z_buy, SELL when z > z_sell.
    Uses deque(maxlen=lookback) per symbol for O(1) rolling window.
    """

    def __init__(
        self,
        strategy_id: str,
        lookback: int = 20,
        z_buy: float = -2.0,
        z_sell: float = 2.0,
        heartbeat_timeout_s: float = 5.0,
    ) -> None:
        super().__init__(strategy_id, heartbeat_timeout_s)
        self.lookback = lookback
        self.z_buy = z_buy
        self.z_sell = z_sell
        self._prices: dict[str, deque] = {}

    def on_tick(self, tick) -> None:
        symbol = tick.symbol
        mid = (tick.bid + tick.ask) / 2

        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self.lookback)
        self._prices[symbol].append(mid)

        window = self._prices[symbol]
        if len(window) < self.lookback:
            return

        arr = np.array(window, dtype=float)
        mean = np.mean(arr)
        std = np.std(arr)
        if std == 0.0:
            return  # static prices — guard ZeroDivisionError (Pitfall 6)

        z = (mid - mean) / std

        if z < self.z_buy:
            self.submit_order(side=1, qty=1, price=int(tick.ask), symbol=symbol)
        elif z > self.z_sell:
            self.submit_order(side=2, qty=1, price=int(tick.bid), symbol=symbol)

    def on_fill(self, exec_report) -> None:
        pass  # Phase 6: fills observed; P&L tracking deferred to Phase 8 dashboard


# ── MLSignalStrategy ──────────────────────────────────────────────────────────

class MLSignalStrategy(BaseStrategy):
    """4-feature HGB ML signal strategy. STRAT-04 + STRAT-05.

    Loads a scikit-learn classifier from STRATEGY_MODEL_PATH env var at on_start()
    (post-fork). Emits confidence-scaled limit orders on predict_proba output.

    Features (must match training order in ml_model_meta.json):
        0: volatility          — rolling std of mid prices (VOL_WINDOW)
        1: spread_ratio        — (ask - bid) / mid
        2: volume_imbalance    — (bid_vol - ask_vol) / (bid_vol + ask_vol)
        3: momentum            — (last_mid - first_mid) / first_mid (MOM_WINDOW)

    Signal rules (THRESHOLD = 0.6):
        proba_up >= 0.6  -> BUY  with qty = max(1, int(BASE_QTY * (proba_up - 0.5) * 2))
        1-proba_up >= 0.6 -> SELL with qty = max(1, int(BASE_QTY * ((1-proba_up) - 0.5) * 2))

    joblib is imported INSIDE on_start() only — never at module level.
    self._feat is pre-allocated (1, 4) float32 — reused each tick for zero-alloc inference.
    """

    VOL_WINDOW = 50
    MOM_WINDOW = 20
    VI_WINDOW  = 20
    THRESHOLD  = 0.6
    BASE_QTY   = 10

    def __init__(self, strategy_id: str, heartbeat_timeout_s: float = 5.0) -> None:
        super().__init__(strategy_id, heartbeat_timeout_s)
        self._model = None
        self._vol_win: dict[str, deque] = {}
        self._mom_win: dict[str, deque] = {}
        self._vi_win:  dict[str, deque] = {}
        self._feat = np.zeros((1, 4), dtype=np.float32)  # pre-allocated, reused each tick

    def on_start(self) -> None:
        import os       # noqa: PLC0415
        import joblib   # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        model_path = os.environ.get("STRATEGY_MODEL_PATH")
        if not model_path:
            raise RuntimeError(
                "STRATEGY_MODEL_PATH env var not set. "
                "Point it to models/ml_signal_model.joblib."
            )
        p = Path(model_path)
        if not p.exists():
            raise RuntimeError(
                f"ML model file not found: {model_path}. "
                "Run scripts/train_ml_model.py or use committed model in models/."
            )
        self._model = joblib.load(str(p))

    def on_tick(self, tick) -> None:
        symbol = tick.symbol
        mid = (tick.bid + tick.ask) / 2.0

        # Initialize deques for new symbol
        if symbol not in self._vol_win:
            self._vol_win[symbol] = deque(maxlen=self.VOL_WINDOW)
        if symbol not in self._mom_win:
            self._mom_win[symbol] = deque(maxlen=self.MOM_WINDOW)
        if symbol not in self._vi_win:
            self._vi_win[symbol] = deque(maxlen=self.VI_WINDOW)

        self._vol_win[symbol].append(mid)
        self._mom_win[symbol].append(mid)
        self._vi_win[symbol].append((tick.bid_size, tick.ask_size))

        # Guard: wait for full vol window before inference
        if len(self._vol_win[symbol]) < self.VOL_WINDOW:
            return

        # Feature 0: volatility — rolling std of mid prices
        self._feat[0, 0] = float(np.std(np.array(self._vol_win[symbol], dtype=np.float64)))

        # Feature 1: spread_ratio
        self._feat[0, 1] = float((tick.ask - tick.bid) / mid) if mid != 0 else 0.0

        # Feature 2: volume_imbalance
        bv = sum(x[0] for x in self._vi_win[symbol])
        av = sum(x[1] for x in self._vi_win[symbol])
        self._feat[0, 2] = float((bv - av) / (bv + av)) if (bv + av) > 0 else 0.0

        # Feature 3: momentum
        p0 = self._mom_win[symbol][0]
        self._feat[0, 3] = float(
            (self._mom_win[symbol][-1] - p0) / p0 if p0 != 0 else 0.0
        )

        proba_up = float(self._model.predict_proba(self._feat)[0, 1])

        if proba_up >= self.THRESHOLD:
            confidence = (proba_up - 0.5) * 2
            qty = max(1, int(self.BASE_QTY * confidence))
            self.submit_order(side=1, qty=qty, price=int(tick.ask), symbol=symbol)
        elif (1.0 - proba_up) >= self.THRESHOLD:
            confidence = ((1.0 - proba_up) - 0.5) * 2
            qty = max(1, int(self.BASE_QTY * confidence))
            self.submit_order(side=2, qty=qty, price=int(tick.bid), symbol=symbol)

    def on_fill(self, exec_report) -> None:
        pass  # Phase 7: fills observed; P&L tracking deferred to Phase 8
