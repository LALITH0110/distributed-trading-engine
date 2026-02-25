"""
tests/test_messaging_smoke.py — Phase 1 loopback smoke test.

Sends one protobuf message of each type over ZeroMQ and verifies
deserialization. Uses multiprocessing.Process to mirror the real
architecture (zmq.Context created after fork, inside process).

All socket creation goes through zmq_factory — no raw ctx.socket() calls.
LINGER is set by the factory from topology config, not hardcoded here.

Run with: pytest tests/test_messaging_smoke.py -v
"""

import multiprocessing
import time

import pytest
import zmq

from proto.messages_pb2 import (
    CancelRequest,
    ExecutionReport,
    ExecType,
    Heartbeat,
    KillSwitch,
    MarketDataTick,
    NewOrderSingle,
    OrderType,
    Side,
)


# ── Fixed test ports (not from topology — avoids conflicts) ───────────────────
_PUB_SUB_PORT = 15555
_PUSH_PULL_PORT = 15558


def _build_all_messages() -> list:
    """Build one test message of each type. Returns [(type_name, serialized), ...]"""
    tick = MarketDataTick()
    tick.schema_version = 1
    tick.timestamp_ns = 1_700_000_000_000_000_000
    tick.symbol = "BTCUSDT"
    tick.bid = 65_000_00
    tick.ask = 65_001_00
    tick.bid_size = 1_000
    tick.ask_size = 500

    order = NewOrderSingle()
    order.schema_version = 1
    order.order_id = "ord-001"
    order.symbol = "BTCUSDT"
    order.side = Side.SIDE_BUY
    order.order_type = OrderType.ORDER_TYPE_LIMIT
    order.price = 65_000_00
    order.quantity = 100
    order.strategy_id = "strat-01"

    exec_rpt = ExecutionReport()
    exec_rpt.schema_version = 1
    exec_rpt.exec_id = "exec-001"
    exec_rpt.order_id = "ord-001"
    exec_rpt.exec_type = ExecType.EXEC_TYPE_FILL
    exec_rpt.fill_price = 65_000_00
    exec_rpt.fill_qty = 100
    exec_rpt.leaves_qty = 0
    exec_rpt.cum_qty = 100
    exec_rpt.avg_px = 65_000_00

    hb = Heartbeat()
    hb.schema_version = 1
    hb.node_id = "feed-handler"
    hb.timestamp_ns = 1_700_000_000_000_000_000
    hb.cpu_pct = 12.5
    hb.mem_pct = 34.2
    hb.orders_processed = 1000

    ks = KillSwitch()
    ks.schema_version = 1
    ks.trigger_timestamp = 1_700_000_000_000_000_000
    ks.reason = "fat_finger_limit_exceeded"
    ks.operator_id = "operator-1"

    cancel = CancelRequest()
    cancel.schema_version = 1
    cancel.order_id = "ord-cancel-001"
    cancel.orig_order_id = "ord-001"
    cancel.symbol = "BTCUSDT"
    cancel.strategy_id = "strat-01"

    return [
        ("MarketDataTick", tick.SerializeToString()),
        ("NewOrderSingle", order.SerializeToString()),
        ("ExecutionReport", exec_rpt.SerializeToString()),
        ("Heartbeat", hb.SerializeToString()),
        ("KillSwitch", ks.SerializeToString()),
        ("CancelRequest", cancel.SerializeToString()),
    ]


# ── PUB / SUB smoke test ──────────────────────────────────────────────────────

def _pub_process(port, ready_event, done_event):
    """Publisher — zmq.Context created AFTER fork (inside this function)."""
    from engine.config import load_topology
    from engine import zmq_factory

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)
    sock = zmq_factory.make_pub(ctx, topo)
    sock.bind(f"tcp://127.0.0.1:{port}")
    ready_event.set()
    time.sleep(0.15)  # wait for subscriber to connect + subscribe

    for _name, payload in _build_all_messages():
        sock.send(payload)

    done_event.wait(timeout=5)
    sock.close()
    ctx.term()


def _sub_process(port, results, ready_event):
    """Subscriber — zmq.Context created AFTER fork. make_sub sets SUBSCRIBE=b'' automatically."""
    from engine.config import load_topology
    from engine import zmq_factory

    ready_event.wait(timeout=5)

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)
    sock = zmq_factory.make_sub(ctx, topo)  # SUBSCRIBE=b"" set by factory
    sock.connect(f"tcp://127.0.0.1:{port}")
    time.sleep(0.1)  # slow-joiner guard

    parsers = [
        MarketDataTick, NewOrderSingle, ExecutionReport,
        Heartbeat, KillSwitch, CancelRequest,
    ]

    received = []
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)

    for i, cls in enumerate(parsers):
        ready = dict(poller.poll(timeout=2000))
        if sock in ready:
            data = sock.recv()
            msg = cls()
            msg.ParseFromString(data)
            received.append((cls.__name__, msg.schema_version))
        else:
            received.append((cls.__name__, "TIMEOUT"))

    results.put(received)
    sock.close()
    ctx.term()


def test_pubsub_all_six_message_types():
    """All 6 proto types round-trip over ZMQ PUB/SUB; schema_version=1 on each."""
    results_q = multiprocessing.Queue()
    ready_event = multiprocessing.Event()
    done_event = multiprocessing.Event()

    pub_p = multiprocessing.Process(
        target=_pub_process, args=(_PUB_SUB_PORT, ready_event, done_event), daemon=True
    )
    sub_p = multiprocessing.Process(
        target=_sub_process, args=(_PUB_SUB_PORT, results_q, ready_event), daemon=True
    )

    pub_p.start()
    sub_p.start()
    sub_p.join(timeout=10)
    done_event.set()
    pub_p.join(timeout=5)

    assert not results_q.empty(), "Subscriber returned no results — likely timed out"
    received = results_q.get()
    assert len(received) == 6, f"Expected 6, got {len(received)}: {received}"

    expected = [
        "MarketDataTick", "NewOrderSingle", "ExecutionReport",
        "Heartbeat", "KillSwitch", "CancelRequest",
    ]
    for (msg_type, schema_ver), exp in zip(received, expected):
        assert msg_type == exp, f"Type mismatch: got {msg_type}, expected {exp}"
        assert schema_ver == 1, f"{msg_type}: schema_version={schema_ver}, expected 1"


# ── PUSH / PULL smoke test ────────────────────────────────────────────────────

def _pull_process(port, results):
    """PULL — zmq.Context created AFTER fork. make_pull sets LINGER from topology."""
    from engine.config import load_topology
    from engine import zmq_factory

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)
    sock = zmq_factory.make_pull(ctx, topo)
    sock.bind(f"tcp://127.0.0.1:{port}")

    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    ready = dict(poller.poll(timeout=3000))
    if sock in ready:
        data = sock.recv()
        order = NewOrderSingle()
        order.ParseFromString(data)
        results.put(("ok", order.order_id, order.schema_version, order.side))
    else:
        results.put(("timeout",))

    sock.close()
    ctx.term()


def _push_process(port):
    """PUSH — zmq.Context created AFTER fork. make_push sets LINGER from topology."""
    from engine.config import load_topology
    from engine import zmq_factory

    topo = load_topology()
    ctx = zmq.Context(io_threads=topo.zmq.io_threads)
    sock = zmq_factory.make_push(ctx, topo)
    sock.connect(f"tcp://127.0.0.1:{port}")
    time.sleep(0.1)  # slow-joiner guard

    order = NewOrderSingle()
    order.schema_version = 1
    order.order_id = "ord-push-001"
    order.symbol = "ETHUSDT"
    order.side = Side.SIDE_SELL
    order.order_type = OrderType.ORDER_TYPE_MARKET
    order.quantity = 50
    order.strategy_id = "strat-02"
    sock.send(order.SerializeToString())

    time.sleep(0.2)
    sock.close()
    ctx.term()


def test_pushpull_neworder_roundtrip():
    """NewOrderSingle round-trips over ZMQ PUSH/PULL; fields verified at PULL side."""
    results_q = multiprocessing.Queue()

    pull_p = multiprocessing.Process(
        target=_pull_process, args=(_PUSH_PULL_PORT, results_q), daemon=True
    )
    push_p = multiprocessing.Process(
        target=_push_process, args=(_PUSH_PULL_PORT,), daemon=True
    )

    pull_p.start()
    time.sleep(0.05)  # pull must bind before push connects
    push_p.start()

    pull_p.join(timeout=5)
    push_p.join(timeout=5)

    assert not results_q.empty(), "PULL process returned no results"
    result = results_q.get()
    assert result[0] == "ok", f"PULL timed out or errored: {result}"
    _status, order_id, schema_ver, side = result
    assert order_id == "ord-push-001"
    assert schema_ver == 1
    assert side == Side.SIDE_SELL
