"""
engine/matching_engine.py — ZeroMQ PULL/PUB matching engine process.

Ingests NewOrderSingle protos via PULL (order_egress), routes through
per-symbol ring buffers, calls lob.submit(), publishes ExecutionReport
protos on PUB (exec_report_pub) with topic = strategy_id bytes.

Includes orphan GC (ME-09): cancels open orders from silent strategies.

"Context after fork" rule
--------------------------
zmq is imported INSIDE _matching_engine_target — after multiprocessing
forks. Raw ctx.socket() is banned; all sockets via zmq_factory.
"""

from __future__ import annotations

import multiprocessing
import time
import uuid

from engine.lob import Order, OrderStatus

# ── Module-level constants ─────────────────────────────────────────────────────
SYMBOLS: list[str] = ["BTCUSDT", "ETHUSDT", "AAPL", "MSFT", "SPY"]

# Proto enum -> string maps (Side and OrderType)
_SIDE_MAP = {1: "BUY", 2: "SELL"}      # Side.SIDE_BUY=1, SIDE_SELL=2
_TYPE_MAP = {1: "LIMIT", 2: "MARKET"}  # OrderType.ORDER_TYPE_LIMIT=1, MARKET=2


# ── Module-level helpers (no zmq) ──────────────────────────────────────────────

def _nos_to_order(nos) -> Order:
    """Convert NewOrderSingle proto to engine.lob.Order dataclass."""
    return Order(
        order_id=nos.order_id,
        symbol=nos.symbol,
        side=_SIDE_MAP.get(nos.side, "BUY"),
        order_type=_TYPE_MAP.get(nos.order_type, "LIMIT"),
        price=int(nos.price),
        qty=int(nos.quantity),
        leaves_qty=int(nos.quantity),
    )


def _publish_exec_report(pub, order: Order, exec_type, strategy_id: str,
                          fill_price: int = 0, fill_qty: int = 0) -> None:
    """Build and publish an ExecutionReport proto."""
    from proto.messages_pb2 import ExecutionReport  # noqa: PLC0415
    er = ExecutionReport()
    er.schema_version = 1
    er.exec_id = str(uuid.uuid4())
    er.order_id = order.order_id
    er.strategy_id = strategy_id
    er.symbol = order.symbol
    er.timestamp_ns = time.time_ns()
    er.exec_type = exec_type
    er.fill_price = fill_price
    er.fill_qty = fill_qty
    er.leaves_qty = order.leaves_qty
    er.cum_qty = order.cum_qty
    er.avg_px = order.avg_px
    pub.send_multipart([strategy_id.encode(), er.SerializeToString()])


def _process_order(nos, lob, pub, order_registry: dict,
                   _arrival_ns=None, _latencies=None, _fill_count=None) -> None:
    """Convert NOS -> Order, submit to LOB, publish exec reports."""
    from proto.messages_pb2 import ExecType  # noqa: PLC0415
    order = _nos_to_order(nos)
    sid = order_registry.get(nos.order_id, nos.strategy_id)

    # ── Latency measurement ────────────────────────────────────────────────────
    if _arrival_ns is not None and _latencies is not None:
        t_recv = _arrival_ns.pop(nos.order_id, None)
        if t_recv is not None:
            _latencies.append(time.time_ns() - t_recv)

    fills = lob.submit(order)
    if fills:
        if _fill_count is not None:
            _fill_count[0] += len(fills)
        for resting_id, incoming_id, fill_qty, fill_price in fills:
            # Resolve each order object and publish exec report
            resting_order = lob._order_map.get(resting_id)
            incoming_order = order  # incoming is the order we just submitted

            if resting_order is not None:
                r_exec_type = (ExecType.EXEC_TYPE_FILL
                               if resting_order.leaves_qty == 0
                               else ExecType.EXEC_TYPE_PARTIAL)
                r_sid = order_registry.get(resting_id, "")
                _publish_exec_report(pub, resting_order, r_exec_type, r_sid,
                                     fill_price, fill_qty)

            i_exec_type = (ExecType.EXEC_TYPE_FILL
                           if incoming_order.leaves_qty == 0
                           else ExecType.EXEC_TYPE_PARTIAL)
            _publish_exec_report(pub, incoming_order, i_exec_type, sid,
                                 fill_price, fill_qty)
    else:
        # No fills — limit order rested in book (or market exhausted liquidity)
        _publish_exec_report(pub, order, ExecType.EXEC_TYPE_NEW, sid)


def _run_orphan_gc(lobs: dict, pub, order_registry: dict,
                   strategy_last_seen: dict, orphan_timeout_ns: int) -> None:
    """Cancel open orders from silent strategies; publish EXEC_TYPE_CANCEL."""
    from proto.messages_pb2 import ExecType  # noqa: PLC0415
    now = time.time_ns()
    for sym, lob in lobs.items():
        for order_id, order in list(lob._order_map.items()):  # snapshot
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                continue
            sid = order_registry.get(order_id)
            if sid is None:
                continue
            last_seen = strategy_last_seen.get(sid, 0)
            if (now - last_seen) > orphan_timeout_ns:
                lob.cancel(order_id)
                _publish_exec_report(pub, order, ExecType.EXEC_TYPE_CANCEL, sid)


def _match_loop(lobs: dict, ring_bufs: dict, pub, order_registry: dict,
                strategy_last_seen: dict, symbols: list[str], topo, stop,
                me_hb_pub=None, _arrival_ns=None, _latencies=None,
                _fill_count=None, _orders_proc=None) -> None:
    """Round-robin drain ring buffers, run GC periodically."""
    from proto.messages_pb2 import Heartbeat  # noqa: PLC0415
    orphan_timeout_ns = int(topo.me.orphan_timeout_s * 1e9)
    gc_interval_ns = int(topo.me.gc_interval_s * 1e9)
    last_gc_ns = time.time_ns()
    _total_orders_processed = 0
    _hb_counter = 0
    _HB_INTERVAL = 1000  # emit heartbeat every N loop iterations

    while not stop.is_set():
        any_work = False
        for sym in symbols:
            rb = ring_bufs[sym]
            lob = lobs[sym]
            while rb:
                nos = rb.popleft()
                any_work = True
                _total_orders_processed += 1
                if _orders_proc is not None:
                    _orders_proc[0] += 1
                _process_order(nos, lob, pub, order_registry,
                               _arrival_ns, _latencies, _fill_count)

        now = time.time_ns()
        if now - last_gc_ns >= gc_interval_ns:
            last_gc_ns = now
            _run_orphan_gc(lobs, pub, order_registry,
                           strategy_last_seen, orphan_timeout_ns)

        if not any_work:
            time.sleep(0.0001)  # 100 µs idle sleep

        _hb_counter += 1
        if _hb_counter >= _HB_INTERVAL and me_hb_pub is not None:
            _hb_counter = 0
            hb = Heartbeat()
            hb.schema_version = 1
            hb.node_id = "matching-engine-0"
            hb.timestamp_ns = time.time_ns()
            hb.orders_processed = _total_orders_processed
            me_hb_pub.send(hb.SerializeToString())


# ── Process target ─────────────────────────────────────────────────────────────

def _matching_engine_target(symbols: list[str]) -> None:
    """multiprocessing.Process target — zmq imported after spawn."""
    import signal           # noqa: PLC0415
    import threading        # noqa: PLC0415
    import zmq              # noqa: PLC0415 — post-spawn: Context-after-fork rule
    from collections import deque                        # noqa: PLC0415
    from engine.config import load_topology              # noqa: PLC0415
    from engine import zmq_factory                       # noqa: PLC0415
    from engine.lob import LimitOrderBook                # noqa: PLC0415
    from proto.messages_pb2 import NewOrderSingle        # noqa: PLC0415

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)

    pull = zmq_factory.make_pull(ctx, topo)
    pull.bind(topo.get_bind_addr("order_egress"))

    pub = zmq_factory.make_pub(ctx, topo)
    pub.bind(topo.get_bind_addr("exec_report_pub"))

    cancel_pull = zmq_factory.make_pull(ctx, topo)
    cancel_pull.bind(topo.get_bind_addr("order_cancel"))

    me_hb_pub = zmq_factory.make_pub(ctx, topo)
    me_hb_pub.bind(topo.get_bind_addr("me_heartbeat_pub"))

    lobs = {sym: LimitOrderBook(sym) for sym in symbols}
    ring_bufs = {sym: deque(maxlen=topo.me.ring_buffer_size) for sym in symbols}
    order_registry: dict[str, str] = {}      # order_id -> strategy_id
    strategy_last_seen: dict[str, int] = {}  # strategy_id -> timestamp_ns

    # ── Metrics state ──────────────────────────────────────────────────────────
    _arrival_ns: dict[str, int] = {}          # order_id -> recv timestamp (ns)
    _latencies: deque = deque(maxlen=200_000)  # per-order latency samples (ns)
    _fill_count: list[int] = [0]
    _orders_proc: list[int] = [0]
    _start_ns: list[int] = [time.time_ns()]

    stop = threading.Event()

    def _sigterm_handler(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    def recv_loop():
        while not stop.is_set():
            try:
                raw = pull.recv()
                nos = NewOrderSingle()
                nos.ParseFromString(raw)
                _arrival_ns[nos.order_id] = time.time_ns()
                order_registry[nos.order_id] = nos.strategy_id
                strategy_last_seen[nos.strategy_id] = time.time_ns()
                ring_bufs[nos.symbol].append(nos)
            except zmq.ContextTerminated:
                break
            except zmq.ZMQError:
                break

    t_recv = threading.Thread(target=recv_loop, daemon=True)
    t_recv.start()

    def cancel_recv_loop():
        from proto.messages_pb2 import CancelRequest, ExecType  # noqa: PLC0415
        while not stop.is_set():
            try:
                raw = cancel_pull.recv()
                cr = CancelRequest()
                cr.ParseFromString(raw)
                # Cancel in whichever LOB holds this order
                for lob in lobs.values():
                    if cr.order_id in lob._order_map:
                        order = lob._order_map[cr.order_id]
                        if order.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                            lob.cancel(cr.order_id)
                            sid = order_registry.get(cr.order_id, "")
                            _publish_exec_report(pub, order, ExecType.EXEC_TYPE_CANCEL, sid)
                        break
            except zmq.ContextTerminated:
                break
            except zmq.ZMQError:
                break

    t_cancel = threading.Thread(target=cancel_recv_loop, daemon=True)
    t_cancel.start()

    try:
        _match_loop(lobs, ring_bufs, pub, order_registry,
                    strategy_last_seen, symbols, topo, stop,
                    me_hb_pub=me_hb_pub,
                    _arrival_ns=_arrival_ns, _latencies=_latencies,
                    _fill_count=_fill_count, _orders_proc=_orders_proc)
    finally:
        # ── Write metrics to file FIRST — ctx.term() may C-abort afterward ─────
        import json as _json
        elapsed_s = (time.time_ns() - _start_ns[0]) / 1e9
        n_orders = _orders_proc[0]
        n_fills = _fill_count[0]
        if n_orders > 0 and elapsed_s > 0:
            throughput = n_orders / elapsed_s
            if _latencies:
                lats_us = sorted(l / 1_000 for l in _latencies)
                _n = len(lats_us)
                def _pct(p):
                    return lats_us[min(int(_n * p / 100), _n - 1)]
                p50  = _pct(50)
                p95  = _pct(95)
                p99  = _pct(99)
                p999 = _pct(99.9)
            else:
                p50 = p95 = p99 = p999 = 0.0
            metrics = {
                "orders": n_orders, "fills": n_fills,
                "elapsed_s": round(elapsed_s, 2),
                "throughput": round(throughput, 1),
                "p50_us": round(p50, 1), "p95_us": round(p95, 1),
                "p99_us": round(p99, 1), "p999_us": round(p999, 1),
            }
            try:
                with open("/tmp/me_metrics.json", "w") as _f:
                    _json.dump(metrics, _f)
                    _f.flush()
            except Exception:
                pass

        pull.close()          # unblocks recv thread (raises ContextTerminated)
        cancel_pull.close()   # unblocks cancel recv thread
        pub.close()
        me_hb_pub.close()
        try:
            ctx.term()        # may hit ZMQ C-level assert — metrics already saved
        except Exception:
            pass


# ── Public API ─────────────────────────────────────────────────────────────────

def start_matching_engine(symbols: list[str] = SYMBOLS) -> multiprocessing.Process:
    """Spawn and return a daemon Process running the matching engine.

    Parameters
    ----------
    symbols : list of symbol strings to create LOBs for.

    Returns
    -------
    multiprocessing.Process — already started, daemon=True.
    """
    p = multiprocessing.Process(
        target=_matching_engine_target,
        args=(symbols,),
        daemon=True,
    )
    p.start()
    return p
