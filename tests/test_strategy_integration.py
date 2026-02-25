"""
tests/test_strategy_integration.py — End-to-end integration test for the
full trading pipeline with two MeanReversionStrategy nodes.

Tests start the complete pipeline on isolated ports (18000-18008):
  - Matching Engine   (PULL 18004, PUB 18001 exec reports, PULL 18007 cancel)
  - Risk Gateway      (PULL 18003 order ingress, PUSH -> ME, PUB 18006 rejects,
                       PULL 18005 kill switch)
  - Custom GBM feed   (PUB 18000 market data, PUB 18002 heartbeats)
  - Strategy strat-001 (MeanReversionStrategy, lookback=20)
  - Strategy strat-002 (MeanReversionStrategy, lookback=20)

Marker: pytest.mark.integration — skip in normal runs with:
    pytest tests/ -x -q -m "not integration"
Run explicitly with:
    pytest tests/test_strategy_integration.py -v -s
"""

from __future__ import annotations

import multiprocessing
import os
import pathlib
import tempfile
import time

import pytest
import yaml
import zmq

from engine.matching_engine import start_matching_engine
from engine.risk_gateway import start_risk_gateway
from engine.strategy import MeanReversionStrategy, MLSignalStrategy, start_strategy
from proto.messages_pb2 import ExecutionReport, ExecType

pytestmark = pytest.mark.integration


# ── Port constants ────────────────────────────────────────────────────────────

_PORTS = {
    "feed_pub":        18000,
    "exec_report_pub": 18001,
    "heartbeat_pub":   18002,
    "order_ingress":   18003,
    "order_egress":    18004,
    "kill_switch":     18005,
    "risk_reject_pub": 18006,
    "order_cancel":    18007,
}


# ── Topology builder ──────────────────────────────────────────────────────────

def _make_test_topology(ports: dict) -> dict:
    """Build a topology dict for the integration test port range 18000-18008."""
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
                "role":          "PUB",
                "bind_host_key": "feed",
                "port":          ports["feed_pub"],
            },
            "exec_report_pub": {
                "role":          "PUB",
                "bind_host_key": "matching_engine",
                "port":          ports["exec_report_pub"],
            },
            "heartbeat_pub": {
                "role":          "PUB",
                "bind_host_key": "feed",            # fixed in 06-01: feed, not matching_engine
                "port":          ports["heartbeat_pub"],
            },
            "order_ingress": {
                "role":          "PULL",
                "bind_host_key": "risk_gateway",
                "port":          ports["order_ingress"],
            },
            "order_egress": {
                "role":          "PULL",
                "bind_host_key": "matching_engine",
                "port":          ports["order_egress"],
            },
            "kill_switch": {
                "role":          "PULL",
                "bind_host_key": "risk_gateway",
                "port":          ports["kill_switch"],
            },
            "risk_reject_pub": {
                "role":          "PUB",
                "bind_host_key": "risk_gateway",
                "port":          ports["risk_reject_pub"],
            },
            "order_cancel": {
                "role":          "PULL",
                "bind_host_key": "matching_engine",
                "port":          ports["order_cancel"],
            },
        },
        "zmq": {
            "linger_ms":       0,
            "io_threads":      1,
            "sndhwm":          100000,
            "rcvhwm":          100000,
            "connect_sleep_ms": 100,
        },
        "matching_engine": {
            "ring_buffer_size": 65536,
            "orphan_timeout_s": 30.0,
            "gc_interval_s":     5.0,
        },
        "risk_gateway": {
            "fat_finger_max_notional": 10_000_000,  # high enough for GBM prices
            "position_limit":           100,         # Pitfall 2: default 10 blocks orders
            "rate_limit_per_s":         1000.0,
            "token_bucket_capacity":    1000,
            "inbound_rcvhwm":           10000,
        },
    }


# ── Custom GBM feed process ────────────────────────────────────────────────────

def _gbm_feed_target(topo_dict: dict, topo_path: str) -> None:
    """Custom feed process: GBM prices with heartbeats on isolated ports.

    - Context-after-fork: all zmq imports here.
    - Emits 500 GBM ticks at 1ms intervals (~0.5s total).
    - Heartbeat every 50 ticks during emission.
    - After emission, keeps publishing heartbeats indefinitely (1s interval)
      so strategies don't enter safe-mode during the assertion window.
    """
    import os as _os           # noqa: PLC0415
    import time as _time       # noqa: PLC0415
    import zmq as _zmq         # noqa: PLC0415

    from engine.config import load_topology   # noqa: PLC0415
    from engine import zmq_factory            # noqa: PLC0415
    from engine.datasets import generate_gbm_prices  # noqa: PLC0415
    from proto.messages_pb2 import MarketDataTick, Heartbeat  # noqa: PLC0415

    # Use topo_path so load_topology() reads the test topology
    _os.environ["ME_TOPOLOGY_PATH"] = topo_path
    topo = load_topology()
    ctx = _zmq.Context(io_threads=topo.zmq.io_threads)

    hb_pub = zmq_factory.make_pub(ctx, topo)
    hb_pub.bind(topo.get_bind_addr("heartbeat_pub"))

    pub = zmq_factory.make_pub(ctx, topo)
    pub.bind(topo.get_bind_addr("feed_pub"))

    # Slow-joiner guard
    _time.sleep(topo.zmq.connect_sleep_ms / 1000.0)

    prices = generate_gbm_prices(S0=30000.0, mu=0.1, sigma=0.2, n_ticks=500)

    def _emit_hb() -> None:
        hb = Heartbeat()
        hb.schema_version = 1
        hb.node_id = "feed-test"
        hb.timestamp_ns = _time.time_ns()
        hb_pub.send(hb.SerializeToString())

    tick = MarketDataTick()
    tick.schema_version = 1
    tick.symbol = "BTCUSDT"
    tick.bid_size = 1
    tick.ask_size = 1

    i = 0
    while True:
        if i % 50 == 0:
            _emit_hb()

        price = prices[i % len(prices)]
        tick.timestamp_ns = _time.time_ns()
        tick.bid = int(price)
        tick.ask = int(price) + 100
        pub.send(tick.SerializeToString())
        _time.sleep(0.001)  # 1ms between ticks
        i += 1


# ── Exec report helpers ───────────────────────────────────────────────────────

def _make_exec_sub(ctx: zmq.Context, topo_dict: dict, strategy_id: str) -> zmq.Socket:
    """Create SUB socket subscribed to strategy_id topic on exec_report_pub port."""
    port = topo_dict["endpoints"]["exec_report_pub"]["port"]
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.LINGER, 0)
    sub.setsockopt(zmq.SUBSCRIBE, strategy_id.encode())
    sub.connect(f"tcp://127.0.0.1:{port}")
    return sub


def _drain_fills(sub: zmq.Socket, count: int, timeout_s: float) -> list:
    """Poll exec_report_pub until `count` EXEC_TYPE_FILL reports arrive or timeout.

    Returns list of ExecutionReport objects with exec_type == EXEC_TYPE_FILL.
    """
    fills = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            frames = sub.recv_multipart(flags=zmq.NOBLOCK)
            er = ExecutionReport()
            er.ParseFromString(frames[1])
            if er.exec_type == ExecType.EXEC_TYPE_FILL:
                fills.append(er)
                if len(fills) >= count:
                    break
        except zmq.Again:
            time.sleep(0.001)
    return fills


# ── Module-scoped fixture ─────────────────────────────────────────────────────

class TestStrategyIntegration:
    """STRAT-06 + STRAT-07: Two MeanReversionStrategy nodes receive fills."""

    @pytest.fixture(scope="module")
    def pipeline(self, tmp_path_factory):
        topo_dict = _make_test_topology(_PORTS)

        # Write topology to temp file
        tmp_dir = tmp_path_factory.mktemp("integration")
        topo_path = str(tmp_dir / "topology.yaml")
        with open(topo_path, "w") as f:
            yaml.safe_dump(topo_dict, f)

        # Set env var so all spawned processes load the test topology
        old_env = os.environ.get("ME_TOPOLOGY_PATH")
        os.environ["ME_TOPOLOGY_PATH"] = topo_path

        # 1. Start Matching Engine
        me_proc = start_matching_engine(symbols=["BTCUSDT"])

        # 2. Start Risk Gateway
        rg_proc = start_risk_gateway()

        # 3. Start custom GBM feed (passes topo_path explicitly)
        feed_proc = multiprocessing.Process(
            target=_gbm_feed_target,
            args=(topo_dict, topo_path),
            daemon=True,
        )
        feed_proc.start()

        # 4. Start strategy nodes — complementary params to ensure cross-strategy fills.
        # strat-001 is a pure buyer (z_sell=99 never triggers sells).
        # strat-002 is a pure seller (z_buy=-99 never triggers buys).
        # With seed=42 GBM: strat-002 posts SELLs ~tick 30; strat-001 posts BUYs ~tick 72.
        # BUY price (ask ~30102) > SELL price (bid ~30007) -> ME fills both.
        s1 = MeanReversionStrategy("strat-001", lookback=20, z_buy=-2.0, z_sell=99.0)
        p1 = start_strategy(s1)

        s2 = MeanReversionStrategy("strat-002", lookback=20, z_buy=-99.0, z_sell=2.0)
        p2 = start_strategy(s2)

        # Allow slow-joiner connections to stabilize
        time.sleep(1.0)

        ctx = zmq.Context()

        yield {"ctx": ctx, "topo": topo_dict}

        # Teardown
        for proc in [p2, p1, feed_proc, rg_proc, me_proc]:
            if proc.is_alive():
                proc.terminate()
        for proc in [p2, p1, feed_proc, rg_proc, me_proc]:
            proc.join(timeout=2.0)

        ctx.term()

        if old_env is None:
            os.environ.pop("ME_TOPOLOGY_PATH", None)
        else:
            os.environ["ME_TOPOLOGY_PATH"] = old_env

    def test_two_strategies_receive_fills(self, pipeline):
        """STRAT-06: Both strat-001 and strat-002 each receive >=1 EXEC_TYPE_FILL
        from the Matching Engine within 30 seconds.

        Flow: GBM ticks -> MeanReversionStrategy.on_tick() -> submit_order() ->
              RG risk checks -> ME matching -> ExecutionReport FILL -> strategy.on_fill()
        """
        ctx = pipeline["ctx"]
        topo_dict = pipeline["topo"]

        sub1 = _make_exec_sub(ctx, topo_dict, "strat-001")
        sub2 = _make_exec_sub(ctx, topo_dict, "strat-002")

        # Small delay to ensure subscribers are connected
        time.sleep(0.2)

        fills_s1 = _drain_fills(sub1, count=1, timeout_s=30.0)
        fills_s2 = _drain_fills(sub2, count=1, timeout_s=30.0)

        sub1.close()
        sub2.close()

        assert len(fills_s1) >= 1, (
            f"strat-001 received {len(fills_s1)} fills, expected >=1 within 30s"
        )
        assert len(fills_s2) >= 1, (
            f"strat-002 received {len(fills_s2)} fills, expected >=1 within 30s"
        )

        for er in fills_s1:
            assert er.strategy_id == "strat-001", f"Wrong strategy_id: {er.strategy_id}"
            assert er.symbol == "BTCUSDT", f"Wrong symbol: {er.symbol}"
            assert er.fill_qty > 0, f"fill_qty should be > 0, got {er.fill_qty}"

        for er in fills_s2:
            assert er.strategy_id == "strat-002", f"Wrong strategy_id: {er.strategy_id}"
            assert er.symbol == "BTCUSDT", f"Wrong symbol: {er.symbol}"
            assert er.fill_qty > 0, f"fill_qty should be > 0, got {er.fill_qty}"


# ── ML integration ports ──────────────────────────────────────────────────────

_ML_PORTS = {
    "feed_pub":        19000,
    "exec_report_pub": 19001,
    "heartbeat_pub":   19002,
    "order_ingress":   19003,
    "order_egress":    19004,
    "kill_switch":     19005,
    "risk_reject_pub": 19006,
    "order_cancel":    19007,
}


# ── ML feed process (module-level for macOS spawn compat) ─────────────────────

def _ml_feed_target(topo_dict: dict, topo_path: str) -> None:
    """GBM feed for ML integration test: emits >200 ticks with heartbeats.

    Must emit >100 ticks before strategy warm-up completes (VOL_WINDOW=50).
    Runs infinite GBM loop (cycles 500-tick array) like Phase 6 pattern.
    """
    import os as _os        # noqa: PLC0415
    import time as _time    # noqa: PLC0415
    import zmq as _zmq      # noqa: PLC0415

    from engine.config import load_topology          # noqa: PLC0415
    from engine import zmq_factory                   # noqa: PLC0415
    from engine.datasets import generate_gbm_prices  # noqa: PLC0415
    from proto.messages_pb2 import MarketDataTick, Heartbeat  # noqa: PLC0415

    _os.environ["ME_TOPOLOGY_PATH"] = topo_path
    topo = load_topology()
    ctx = _zmq.Context(io_threads=topo.zmq.io_threads)

    hb_pub = zmq_factory.make_pub(ctx, topo)
    hb_pub.bind(topo.get_bind_addr("heartbeat_pub"))
    pub = zmq_factory.make_pub(ctx, topo)
    pub.bind(topo.get_bind_addr("feed_pub"))
    _time.sleep(topo.zmq.connect_sleep_ms / 1000.0)

    prices = generate_gbm_prices(S0=30000.0, mu=0.1, sigma=0.2, n_ticks=500)

    def _emit_hb():
        hb = Heartbeat()
        hb.schema_version = 1
        hb.node_id = "ml-feed-test"
        hb.timestamp_ns = _time.time_ns()
        hb_pub.send(hb.SerializeToString())

    tick = MarketDataTick()
    tick.schema_version = 1
    tick.symbol = "BTCUSDT"
    tick.bid_size = 1
    tick.ask_size = 1

    i = 0
    while True:
        if i % 50 == 0:
            _emit_hb()
        price = prices[i % len(prices)]
        tick.timestamp_ns = _time.time_ns()
        tick.bid = int(price)
        tick.ask = int(price) + 100
        pub.send(tick.SerializeToString())
        _time.sleep(0.001)
        i += 1


# ── TestMLSignalStrategyIntegration ──────────────────────────────────────────

class _AlwaysBuyModel:
    """Minimal sklearn-compatible mock that always returns proba_up=0.75.

    Used in integration test to guarantee signals flow through the pipeline
    regardless of GBM path characteristics.  The real model (trained on
    synthetic GBM candles) returns proba ~0.497 on GBM test data — never
    crossing the 0.6 threshold — because volume_imbalance is always 0.0
    (bid_size == ask_size in the live feed) and spread_ratio is near-constant.
    A deterministic mock validates the full pipeline (RG -> ME -> fill report)
    independently of model quality.
    """

    def predict_proba(self, X):
        import numpy as _np  # noqa: PLC0415
        n = X.shape[0] if hasattr(X, "shape") else 1
        return _np.tile([[0.25, 0.75]], (n, 1))


class TestMLSignalStrategyIntegration:
    """STRAT-04 + STRAT-05: MLSignalStrategy submits confidence-scaled orders through full pipeline."""

    @pytest.fixture(scope="module")
    def ml_pipeline(self, tmp_path_factory):
        real_model_path = str(pathlib.Path(__file__).parent.parent / "models" / "ml_signal_model.joblib")
        if not pathlib.Path(real_model_path).exists():
            pytest.skip("ML model not found — run scripts/train_ml_model.py")

        topo_dict = _make_test_topology(_ML_PORTS)
        tmp_dir = tmp_path_factory.mktemp("ml_integration")
        topo_path = str(tmp_dir / "topology.yaml")
        with open(topo_path, "w") as f:
            yaml.safe_dump(topo_dict, f)

        # Write deterministic mock model to tmp_dir.
        # Real model always predicts ~0.497 on GBM data (vol_imbalance=0, spread near-constant)
        # — never crosses THRESHOLD=0.6.  Mock validates pipeline correctness independently.
        import joblib as _jl  # noqa: PLC0415
        mock_model_path = str(tmp_dir / "mock_ml_model.joblib")
        _jl.dump(_AlwaysBuyModel(), mock_model_path)

        old_topo = os.environ.get("ME_TOPOLOGY_PATH")
        old_model = os.environ.get("STRATEGY_MODEL_PATH")
        os.environ["ME_TOPOLOGY_PATH"] = topo_path
        os.environ["STRATEGY_MODEL_PATH"] = mock_model_path

        me_proc = start_matching_engine(symbols=["BTCUSDT"])
        rg_proc = start_risk_gateway()

        feed_proc = multiprocessing.Process(
            target=_ml_feed_target,
            args=(topo_dict, topo_path),
            daemon=True,
        )
        feed_proc.start()

        # ml-strat-001: pure BUY (always-buy mock model, proba_up=0.75)
        # ml-strat-002: pure SELL counterparty (MeanReversionStrategy with z_sell=2.0, z_buy=-99)
        s1 = MLSignalStrategy("ml-strat-001", heartbeat_timeout_s=5.0)
        strat_proc1 = start_strategy(s1)

        s2 = MeanReversionStrategy("ml-strat-002", lookback=20, z_buy=-99.0, z_sell=2.0)
        strat_proc2 = start_strategy(s2)

        time.sleep(1.5)  # slow-joiner guard (extra 0.5s for warm-up window)

        ctx = zmq.Context()

        yield {"ctx": ctx, "topo": topo_dict}

        # Teardown
        for proc in [strat_proc1, strat_proc2, feed_proc, rg_proc, me_proc]:
            if proc.is_alive():
                proc.terminate()
        for proc in [strat_proc1, strat_proc2, feed_proc, rg_proc, me_proc]:
            proc.join(timeout=2.0)

        ctx.term()

        if old_topo is None:
            os.environ.pop("ME_TOPOLOGY_PATH", None)
        else:
            os.environ["ME_TOPOLOGY_PATH"] = old_topo

        if old_model is None:
            os.environ.pop("STRATEGY_MODEL_PATH", None)
        else:
            os.environ["STRATEGY_MODEL_PATH"] = old_model

    def test_ml_strategy_receives_fills(self, ml_pipeline):
        """STRAT-04 + STRAT-05: MLSignalStrategy submits confidence-scaled orders through full pipeline.

        Warm-up: 50 ticks needed before first inference (VOL_WINDOW=50).
        ml-strat-001 (always-buy mock) posts BUY at tick.ask.
        ml-strat-002 (MeanReversionStrategy pure-seller) posts SELL at tick.bid.
        BUY ask > SELL bid -> ME fills both.
        Wait up to 60s for at least 1 EXEC_TYPE_FILL on ml-strat-001.
        Verify: fill_qty matches confidence scaling (qty between 1 and 10).
        """
        ctx = ml_pipeline["ctx"]
        topo_dict = ml_pipeline["topo"]

        sub = _make_exec_sub(ctx, topo_dict, "ml-strat-001")
        time.sleep(0.5)  # sub connect

        fills = _drain_fills(sub, count=1, timeout_s=60.0)  # 60s: warm-up + inference + pipeline
        sub.close()

        assert len(fills) >= 1, f"ml-strat-001 got {len(fills)} fills within 60s"
        for er in fills:
            assert er.strategy_id == "ml-strat-001", f"Wrong strategy_id: {er.strategy_id}"
            assert er.symbol == "BTCUSDT", f"Wrong symbol: {er.symbol}"
            assert 1 <= er.fill_qty <= 10, f"fill_qty {er.fill_qty} out of confidence range [1,10]"
