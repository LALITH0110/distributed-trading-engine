"""
tests/test_matching_engine.py — Integration test suite for the matching engine.

Tests run the ME as a real subprocess via start_matching_engine(), send actual
ZMQ messages, and receive real ExecutionReport protos.

Port assignments (isolated from default 5555-5560 topology ports):
  TestDirectPushToME:  16000-16003
  TestOrphanGC:        16004-16007
  TestCleanShutdown:   16008-16010
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid

import pytest
import yaml
import zmq

from engine.matching_engine import start_matching_engine
from proto.messages_pb2 import (
    ExecutionReport,
    ExecType,
    NewOrderSingle,
    Side,
    OrderType,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_test_topology(
    order_egress_port: int,
    exec_report_pub_port: int,
    orphan_timeout_s: float = 10.0,
    gc_interval_s: float = 5.0,
    order_cancel_port: int = 16095,
) -> dict:
    """Build a minimal topology dict for test port isolation."""
    return {
        "deployment_mode": "local",
        "hosts": {
            "local": {
                "feed":            "127.0.0.1",
                "matching_engine": "127.0.0.1",
                "risk_gateway":    "127.0.0.1",
                "dashboard":       "127.0.0.1",
            },
            "fabric": {
                "feed":            "${FABRIC_FEED_HOST}",
                "matching_engine": "${FABRIC_ME_HOST}",
                "risk_gateway":    "${FABRIC_RISK_HOST}",
                "dashboard":       "${FABRIC_DASH_HOST}",
            },
        },
        "endpoints": {
            "feed_pub": {
                "role": "PUB",
                "bind_host_key": "feed",
                "port": 16099,
            },
            "exec_report_pub": {
                "role": "PUB",
                "bind_host_key": "matching_engine",
                "port": exec_report_pub_port,
            },
            "heartbeat_pub": {
                "role": "PUB",
                "bind_host_key": "matching_engine",
                "port": 16098,
            },
            "order_ingress": {
                "role": "PULL",
                "bind_host_key": "risk_gateway",
                "port": 16097,
            },
            "order_egress": {
                "role": "PULL",
                "bind_host_key": "matching_engine",
                "port": order_egress_port,
            },
            "kill_switch": {
                "role": "PULL",
                "bind_host_key": "risk_gateway",
                "port": 16096,
            },
            "risk_reject_pub": {
                "role": "PUB",
                "bind_host_key": "risk_gateway",
                "port": 16094,
            },
            "order_cancel": {
                "role": "PULL",
                "bind_host_key": "matching_engine",
                "port": order_cancel_port,
            },
            "me_heartbeat_pub": {
                "role": "PUB",
                "bind_host_key": "matching_engine",
                "port": order_cancel_port + 1,
            },
            "rg_heartbeat_pub": {
                "role": "PUB",
                "bind_host_key": "risk_gateway",
                "port": order_cancel_port + 2,
            },
        },
        "zmq": {
            "linger_ms": 0,
            "io_threads": 1,
            "sndhwm": 100000,
            "rcvhwm": 100000,
            "connect_sleep_ms": 100,
        },
        "matching_engine": {
            "ring_buffer_size": 65536,
            "orphan_timeout_s": orphan_timeout_s,
            "gc_interval_s": gc_interval_s,
        },
    }


def _make_nos(
    symbol: str,
    side: int,
    price: int,
    qty: int = 1,
    strategy_id: str = "STRAT-1",
    order_id: str | None = None,
) -> NewOrderSingle:
    """Build a NewOrderSingle proto."""
    nos = NewOrderSingle()
    nos.schema_version = 1
    nos.order_id = order_id or str(uuid.uuid4())
    nos.symbol = symbol
    nos.side = side
    nos.order_type = OrderType.ORDER_TYPE_LIMIT
    nos.price = price
    nos.quantity = qty
    nos.strategy_id = strategy_id
    return nos


def _drain_exec_reports(sub_sock, count: int, timeout_s: float) -> list:
    """Drain ExecutionReport protos from SUB socket until count or timeout."""
    reports = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            frames = sub_sock.recv_multipart(flags=zmq.NOBLOCK)
            # frames[0] = topic, frames[1] = serialized ER
            er = ExecutionReport()
            er.ParseFromString(frames[1])
            reports.append(er)
            if len(reports) >= count:
                break
        except zmq.Again:
            time.sleep(0.001)
    return reports


def _drain_by_exec_type(sub_sock, exec_type_val: int, count: int, timeout_s: float) -> list:
    """Drain exec reports of a specific exec_type until count or timeout."""
    reports = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            frames = sub_sock.recv_multipart(flags=zmq.NOBLOCK)
            er = ExecutionReport()
            er.ParseFromString(frames[1])
            if er.exec_type == exec_type_val:
                reports.append(er)
            if len(reports) >= count:
                break
        except zmq.Again:
            time.sleep(0.005)
    return reports


# ── Class TestDirectPushToME ───────────────────────────────────────────────────

class TestDirectPushToME:
    """ME-07: PUSH orders -> ME PULL -> SUB receives ExecutionReports."""

    @pytest.fixture(scope="class", autouse=True)
    def me_process(self):
        topo = _make_test_topology(
            order_egress_port=16001,
            exec_report_pub_port=16002,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(topo, f)
            topo_path = f.name

        env = os.environ.copy()
        env["ME_TOPOLOGY_PATH"] = topo_path

        # Patch env before spawning so subprocess picks it up
        old_env = os.environ.get("ME_TOPOLOGY_PATH")
        os.environ["ME_TOPOLOGY_PATH"] = topo_path

        proc = start_matching_engine(symbols=["BTCUSDT"])
        time.sleep(0.3)  # allow ME to bind sockets

        yield proc

        proc.terminate()
        proc.join(timeout=2.0)
        if old_env is None:
            os.environ.pop("ME_TOPOLOGY_PATH", None)
        else:
            os.environ["ME_TOPOLOGY_PATH"] = old_env
        os.unlink(topo_path)

    def test_1000_orders_all_receive_exec_reports(self):
        ctx = zmq.Context()
        push = ctx.socket(zmq.PUSH)
        push.setsockopt(zmq.LINGER, 0)
        push.connect("tcp://127.0.0.1:16001")

        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.SUBSCRIBE, b"STRAT-1")
        sub.connect("tcp://127.0.0.1:16002")

        time.sleep(0.1)  # slow joiner guard

        n = 1000
        for i in range(n):
            # Alternate BUY at 31000 / SELL at 29000 — crossing prices guarantee fills
            if i % 2 == 0:
                side = Side.SIDE_BUY
                price = 31000
            else:
                side = Side.SIDE_SELL
                price = 29000
            nos = _make_nos("BTCUSDT", side, price, qty=1, strategy_id="STRAT-1")
            push.send(nos.SerializeToString())

        reports = _drain_exec_reports(sub, n, timeout_s=5.0)

        push.close()
        sub.close()
        ctx.term()

        assert len(reports) >= n, (
            f"Expected >= {n} exec reports, got {len(reports)}"
        )
        valid_exec_types = {
            ExecType.EXEC_TYPE_NEW,
            ExecType.EXEC_TYPE_PARTIAL,
            ExecType.EXEC_TYPE_FILL,
        }
        for er in reports:
            assert er.strategy_id == "STRAT-1", f"Bad strategy_id: {er.strategy_id}"
            assert er.symbol == "BTCUSDT", f"Bad symbol: {er.symbol}"
            assert er.timestamp_ns > 0, f"timestamp_ns not set"
            assert er.exec_type in valid_exec_types, f"Unexpected exec_type: {er.exec_type}"


# ── Class TestOrphanGC ─────────────────────────────────────────────────────────

class TestOrphanGC:
    """ME-09: Orders from silent strategies are CANCELLED after orphan_timeout_s."""

    ORPHAN_TIMEOUT = 1.0
    GC_INTERVAL = 0.5

    @pytest.fixture(scope="class", autouse=True)
    def me_process(self):
        topo = _make_test_topology(
            order_egress_port=16005,
            exec_report_pub_port=16006,
            orphan_timeout_s=self.ORPHAN_TIMEOUT,
            gc_interval_s=self.GC_INTERVAL,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(topo, f)
            topo_path = f.name

        old_env = os.environ.get("ME_TOPOLOGY_PATH")
        os.environ["ME_TOPOLOGY_PATH"] = topo_path

        proc = start_matching_engine(symbols=["BTCUSDT"])
        time.sleep(0.3)

        yield proc

        proc.terminate()
        proc.join(timeout=2.0)
        if old_env is None:
            os.environ.pop("ME_TOPOLOGY_PATH", None)
        else:
            os.environ["ME_TOPOLOGY_PATH"] = old_env
        os.unlink(topo_path)

    def test_orphaned_orders_cancelled_after_timeout(self):
        ctx = zmq.Context()
        push = ctx.socket(zmq.PUSH)
        push.setsockopt(zmq.LINGER, 0)
        push.connect("tcp://127.0.0.1:16005")

        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.SUBSCRIBE, b"STRAT-DEAD")
        sub.connect("tcp://127.0.0.1:16006")

        time.sleep(0.1)  # slow joiner guard

        # Send 5 LIMIT BUY orders at price=1 (far below any sell — will rest in book)
        n_orders = 5
        for _ in range(n_orders):
            nos = _make_nos("BTCUSDT", Side.SIDE_BUY, price=1, qty=1,
                            strategy_id="STRAT-DEAD")
            push.send(nos.SerializeToString())

        # Wait for EXEC_TYPE_NEW acknowledgements (5 expected)
        new_reports = _drain_by_exec_type(
            sub, ExecType.EXEC_TYPE_NEW, n_orders, timeout_s=2.0
        )
        assert len(new_reports) == n_orders, (
            f"Expected {n_orders} EXEC_TYPE_NEW, got {len(new_reports)}"
        )

        # Wait for orphan GC to fire: orphan_timeout_s + gc_interval_s + 0.5s buffer
        wait_s = self.ORPHAN_TIMEOUT + self.GC_INTERVAL + 0.5
        time.sleep(wait_s)

        # Drain for EXEC_TYPE_CANCEL reports
        cancel_reports = _drain_by_exec_type(
            sub, ExecType.EXEC_TYPE_CANCEL, n_orders, timeout_s=2.0
        )

        push.close()
        sub.close()
        ctx.term()

        assert len(cancel_reports) == n_orders, (
            f"Expected {n_orders} EXEC_TYPE_CANCEL, got {len(cancel_reports)}"
        )
        for er in cancel_reports:
            assert er.strategy_id == "STRAT-DEAD", f"Bad strategy_id: {er.strategy_id}"


# ── Class TestCleanShutdown ────────────────────────────────────────────────────

class TestCleanShutdown:
    """DEPLOY-04: ctx.term() completes within 500ms on SIGTERM."""

    @pytest.fixture(scope="class", autouse=True)
    def me_process(self):
        topo = _make_test_topology(
            order_egress_port=16009,
            exec_report_pub_port=16010,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(topo, f)
            topo_path = f.name

        old_env = os.environ.get("ME_TOPOLOGY_PATH")
        os.environ["ME_TOPOLOGY_PATH"] = topo_path

        proc = start_matching_engine(symbols=["BTCUSDT"])
        time.sleep(0.2)  # init

        yield proc

        # If test already terminated the proc, join gracefully
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)
        if old_env is None:
            os.environ.pop("ME_TOPOLOGY_PATH", None)
        else:
            os.environ["ME_TOPOLOGY_PATH"] = old_env
        os.unlink(topo_path)

    def test_sigterm_completes_within_500ms(self, me_process):
        t0 = time.monotonic()
        me_process.terminate()       # SIGTERM
        me_process.join(timeout=1.0)
        elapsed = time.monotonic() - t0

        print(f"\n[shutdown timing] elapsed={elapsed*1000:.1f}ms")
        assert me_process.exitcode is not None, "Process did not exit after SIGTERM"
        assert elapsed < 0.5, (
            f"DEPLOY-04 violated: ME took {elapsed*1000:.1f}ms to shut down (limit 500ms)"
        )
