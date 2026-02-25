"""
engine/risk_gateway.py — Pre-trade risk gateway process.

Inline between strategy nodes (PUSH) and matching engine (PULL).
Enforces: fat-finger, position limits, rate limiter, kill switch.
Risk rejects published via PUB socket (topic = strategy_id).

Context-after-fork: zmq imported inside _risk_gateway_target only.
"""
from __future__ import annotations
import multiprocessing
import time
import uuid

from engine.matching_engine import SYMBOLS

# ── Risk filter helpers (no zmq, unit-testable) ────────────────────────────


def check_fat_finger(nos, max_notional: float) -> bool:
    """Return True if order passes (notional <= max_notional). RISK-01.
    notional = price * quantity. price is in integer ticks (same units as LOB).
    Treat price ticks as dollars for notional calculation per spec.
    """
    notional = nos.price * nos.quantity
    return notional <= max_notional


def check_position_limit(nos, positions: dict, limit: int) -> bool:
    """Return True if order passes position limit check. RISK-02.
    positions: dict[(strategy_id, symbol) -> net_position (signed int)]
    delta = +qty for BUY (side=1), -qty for SELL (side=2).
    Rejects if abs(current + delta) > limit.
    """
    delta = nos.quantity if nos.side == 1 else -nos.quantity
    key = (nos.strategy_id, nos.symbol)
    current = positions.get(key, 0)
    return abs(current + delta) <= limit


def check_rate_limit(nos, buckets: dict, rate: float, capacity: int, now_s: float) -> bool:
    """Token bucket per strategy_id. Return True if token consumed. RISK-03.
    buckets: dict[strategy_id -> [tokens: float, last_refill_s: float]]
    Refills at `rate` tokens/sec up to `capacity`. Costs 1 token per order.
    """
    sid = nos.strategy_id
    if sid not in buckets:
        buckets[sid] = [float(capacity), now_s]
    tokens, last = buckets[sid]
    elapsed = now_s - last
    tokens = min(capacity, tokens + elapsed * rate)
    buckets[sid][1] = now_s
    if tokens >= 1.0:
        buckets[sid][0] = tokens - 1.0
        return True
    buckets[sid][0] = tokens
    return False


def update_position(nos, positions: dict) -> None:
    """Update position ledger after confirmed fill. RISK-02 reconciliation.
    Called when ExecutionReport with exec_type FILL or PARTIAL arrives.
    Requires order_side_registry[order_id] -> side (1=BUY, 2=SELL) to get side.
    """
    # Called with resolved side from order_side_registry
    pass  # Implemented inline in event loop for access to order_side_registry


def _build_reject_er(nos, reason: str) -> bytes:
    from proto.messages_pb2 import ExecutionReport, ExecType  # noqa: PLC0415
    er = ExecutionReport()
    er.schema_version = 1
    er.exec_id = str(uuid.uuid4())
    er.order_id = nos.order_id
    er.strategy_id = nos.strategy_id
    er.symbol = nos.symbol
    er.timestamp_ns = time.time_ns()
    er.exec_type = ExecType.EXEC_TYPE_REJECT  # value=5
    er.fill_price = 0
    er.fill_qty = 0
    er.leaves_qty = nos.quantity
    er.cum_qty = 0
    er.avg_px = 0
    return er.SerializeToString()


# ── Process target ─────────────────────────────────────────────────────────────

def _risk_gateway_target(symbols: list[str]) -> None:
    """multiprocessing.Process target — zmq imported after spawn."""
    import signal           # noqa: PLC0415
    import threading        # noqa: PLC0415
    import zmq              # noqa: PLC0415 — post-spawn: Context-after-fork rule
    from engine.config import load_topology   # noqa: PLC0415
    from engine import zmq_factory            # noqa: PLC0415

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)

    # Inbound: strategies PUSH orders here
    inbound = zmq_factory.make_pull(ctx, topo)
    inbound.setsockopt(zmq.RCVHWM, topo.rg.inbound_rcvhwm)  # RISK-06 override
    inbound.bind(topo.get_bind_addr("order_ingress"))

    # Outbound: forward approved orders to ME
    outbound = zmq_factory.make_push(ctx, topo)
    outbound.connect(topo.get_connect_addr("order_egress"))

    # Risk reject PUB: send REJECT ERs to originating strategy (topic = strategy_id)
    risk_pub = zmq_factory.make_pub(ctx, topo)
    risk_pub.bind(topo.get_bind_addr("risk_reject_pub"))

    # Cancel PUSH: send CancelRequest to ME on kill switch
    cancel_push = zmq_factory.make_push(ctx, topo)
    cancel_push.connect(topo.get_connect_addr("order_cancel"))

    # Exec report SUB: reconcile fills into position ledger
    exec_sub = zmq_factory.make_sub(ctx, topo, topic=b"")
    exec_sub.connect(topo.get_connect_addr("exec_report_pub"))

    # Kill switch PULL: dashboard PUSHes KillSwitch here
    kill_sub = zmq_factory.make_pull(ctx, topo)
    kill_sub.bind(topo.get_bind_addr("kill_switch"))

    # Heartbeat PUB: dashboard subscribes for connectivity detection (DASH-01)
    rg_hb_pub = zmq_factory.make_pub(ctx, topo)
    rg_hb_pub.bind(topo.get_bind_addr("rg_heartbeat_pub"))

    # ── State ──────────────────────────────────────────────────────────────
    positions: dict[tuple[str, str], int] = {}         # (strategy_id, symbol) -> net signed position
    order_side_registry: dict[str, int] = {}            # order_id -> side (1=BUY, 2=SELL)
    pending_orders: dict[str, tuple[str, str]] = {}     # order_id -> (strategy_id, symbol)
    buckets: dict[str, list] = {}                       # rate limiter per strategy
    kill_active: bool = False
    _rg_hb_counter = 0
    _RG_HB_INTERVAL = 1000  # emit heartbeat every N poll cycles

    stop = threading.Event()

    def _sigterm_handler(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _sigterm_handler)

    poller = zmq.Poller()
    poller.register(inbound, zmq.POLLIN)
    poller.register(exec_sub, zmq.POLLIN)
    poller.register(kill_sub, zmq.POLLIN)

    try:
        while not stop.is_set():
            socks = dict(poller.poll(timeout=50))  # 50ms timeout
            now_s = time.monotonic()

            # ── Kill switch ────────────────────────────────────────────────
            if kill_sub in socks:
                kill_sub.recv()
                kill_active = True
                # Send CancelRequest for every pending order to ME
                from proto.messages_pb2 import CancelRequest  # noqa: PLC0415
                for order_id, (sid, sym) in list(pending_orders.items()):
                    cr = CancelRequest()
                    cr.order_id = order_id
                    cr.strategy_id = sid
                    cr.symbol = sym
                    cancel_push.send(cr.SerializeToString())
                pending_orders.clear()

            # ── Fill reconciliation ────────────────────────────────────────
            if exec_sub in socks:
                raw = exec_sub.recv_multipart()
                from proto.messages_pb2 import ExecutionReport, ExecType  # noqa
                er = ExecutionReport()
                er.ParseFromString(raw[1])
                if er.exec_type in (ExecType.EXEC_TYPE_FILL, ExecType.EXEC_TYPE_PARTIAL):
                    side = order_side_registry.get(er.order_id, 1)
                    delta = er.fill_qty if side == 1 else -er.fill_qty
                    key = (er.strategy_id, er.symbol)
                    positions[key] = positions.get(key, 0) + delta
                if er.exec_type == ExecType.EXEC_TYPE_FILL:
                    pending_orders.pop(er.order_id, None)
                    order_side_registry.pop(er.order_id, None)

            # ── Inbound order ──────────────────────────────────────────────
            if inbound in socks:
                raw = inbound.recv()
                from proto.messages_pb2 import NewOrderSingle  # noqa: PLC0415
                nos = NewOrderSingle()
                nos.ParseFromString(raw)

                if nos.side == 0 or nos.quantity == 0:
                    # CancelRequest on order_ingress — forward raw bytes to ME
                    cancel_push.send(raw)
                    continue  # skip NOS processing

                reject_reason = None
                if kill_active:
                    reject_reason = "KILL_SWITCH"
                elif not check_fat_finger(nos, topo.rg.fat_finger_max_notional):
                    reject_reason = "FAT_FINGER"
                elif not check_position_limit(nos, positions, topo.rg.position_limit):
                    reject_reason = "POSITION_LIMIT"
                elif not check_rate_limit(nos, buckets, topo.rg.rate_limit_per_s,
                                          topo.rg.token_bucket_capacity, now_s):
                    reject_reason = "RATE_LIMIT"

                if reject_reason:
                    er_bytes = _build_reject_er(nos, reject_reason)
                    risk_pub.send_multipart([nos.strategy_id.encode(), er_bytes])
                else:
                    order_side_registry[nos.order_id] = nos.side
                    pending_orders[nos.order_id] = (nos.strategy_id, nos.symbol)
                    outbound.send(raw)  # forward raw bytes unchanged — no re-serialize

            # ── Heartbeat emission ─────────────────────────────────────────
            _rg_hb_counter += 1
            if _rg_hb_counter >= _RG_HB_INTERVAL:
                _rg_hb_counter = 0
                from proto.messages_pb2 import Heartbeat  # noqa: PLC0415
                hb = Heartbeat()
                hb.schema_version = 1
                hb.node_id = "risk-gateway-0"
                hb.timestamp_ns = time.time_ns()
                rg_hb_pub.send(hb.SerializeToString())
    finally:
        inbound.close()
        outbound.close()
        risk_pub.close()
        cancel_push.close()
        exec_sub.close()
        kill_sub.close()
        rg_hb_pub.close()
        ctx.term()


# ── Public API ─────────────────────────────────────────────────────────────────

def start_risk_gateway(symbols: list[str] = SYMBOLS) -> multiprocessing.Process:
    """Spawn and return a daemon Process running the risk gateway.

    Parameters
    ----------
    symbols : list of symbol strings (passed to event loop for context).

    Returns
    -------
    multiprocessing.Process — already started, daemon=True.
    """
    p = multiprocessing.Process(
        target=_risk_gateway_target,
        args=(symbols,),
        daemon=True,
    )
    p.start()
    return p
