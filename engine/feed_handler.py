"""
engine/feed_handler.py — ZeroMQ PUB feed process broadcasting MarketDataTick protos.

Three modes
-----------
gbm     : GBM-style synthetic ticks at ~10K ticks/sec across 5 assets (2K each).
          Uses an absolute monotonic_ns rate gate to avoid drift accumulation.
stress  : No rate gate — spins as fast as the CPU allows.  Target: 50K+ ticks/sec.
replay  : Historical replay from dataset files (not yet implemented; see plan 02-02).

"Context after fork" rule
--------------------------
zmq.Context() MUST be created inside the subprocess target — i.e. after
multiprocessing forks.  Creating a Context before fork corrupts the I/O threads
in the child.  Accordingly `import zmq` is done inside _feed_handler_target,
not at module level.
"""

from __future__ import annotations

import multiprocessing
import time

from engine.config import load_topology
from engine import zmq_factory
from proto.messages_pb2 import MarketDataTick

# ── Module-level constants ─────────────────────────────────────────────────────
SYMBOLS: list[str] = ["BTCUSDT", "ETHUSDT", "AAPL", "MSFT", "SPY"]
BASE_PRICES: list[float] = [30000.0, 2000.0, 150.0, 280.0, 400.0]

_TOTAL_RATE = 10_000          # ticks/sec across all symbols
_N_SYMBOLS  = len(SYMBOLS)
HEARTBEAT_INTERVAL = 1000   # emit Heartbeat every N ticks


# ── Internal emit loops ────────────────────────────────────────────────────────

def _run_gbm_loop(pub, topo, hb_pub) -> None:
    """Emit MarketDataTick at ~10K ticks/sec (2K per symbol), GBM price walk."""
    import random
    from proto.messages_pb2 import Heartbeat  # noqa: PLC0415 — post-fork import
    interval_ns = 1_000_000_000 // _TOTAL_RATE   # ns between ticks overall
    slot_ns     = interval_ns // _N_SYMBOLS       # ns per per-symbol slot

    # GBM parameters: mu=0 (drift-free), sigma=0.0002 per tick (~2bps)
    _sigma = 0.0002
    _gauss = random.gauss
    prices = list(BASE_PRICES)  # mutable copy for random walk

    tick = MarketDataTick()
    tick.schema_version = 1
    tick.bid_size = 1
    tick.ask_size = 1

    next_ns = time.monotonic_ns()
    i = 0
    while True:
        now  = time.monotonic_ns()
        wait = next_ns - now
        if wait > 0:
            time.sleep(wait / 1e9)

        idx = i % _N_SYMBOLS
        # GBM step: dS = S * sigma * N(0,1)
        prices[idx] *= (1.0 + _sigma * _gauss(0, 1))
        tick.timestamp_ns = time.time_ns()
        tick.symbol        = SYMBOLS[idx]
        tick.bid           = int(prices[idx] * 100)
        tick.ask           = tick.bid + 100   # 1.00 spread in hundredths
        pub.send(tick.SerializeToString())

        if i % HEARTBEAT_INTERVAL == 0:
            hb = Heartbeat()
            hb.schema_version = 1
            hb.node_id = "feed-0"
            hb.timestamp_ns = time.time_ns()
            hb_pub.send(hb.SerializeToString())

        next_ns += slot_ns
        i += 1


def _run_stress_loop(pub, topo, hb_pub) -> None:
    """Emit MarketDataTick as fast as possible — no sleep, no rate gate."""
    import random
    from proto.messages_pb2 import Heartbeat  # noqa: PLC0415 — post-fork import

    _sigma = 0.0002
    _gauss = random.gauss
    prices = list(BASE_PRICES)

    tick = MarketDataTick()
    tick.schema_version = 1
    tick.bid_size = 1
    tick.ask_size = 1

    i = 0
    while True:
        idx = i % _N_SYMBOLS
        prices[idx] *= (1.0 + _sigma * _gauss(0, 1))
        tick.timestamp_ns = time.time_ns()
        tick.symbol        = SYMBOLS[idx]
        tick.bid           = int(prices[idx] * 100)
        tick.ask           = tick.bid + 100
        pub.send(tick.SerializeToString())

        if i % HEARTBEAT_INTERVAL == 0:
            hb = Heartbeat()
            hb.schema_version = 1
            hb.node_id = "feed-0"
            hb.timestamp_ns = time.time_ns()
            hb_pub.send(hb.SerializeToString())

        i += 1


def _run_replay_loop(pub, topo, speed: float = 1.0) -> None:
    """Historical replay using wall-clock-rate emitter from datasets.replay_candles.
    Loads BTC/USDT May 2021 candles from CSV cache.
    Raises FileNotFoundError if data/btc_usdt_may2021.csv not fetched yet.
    """
    from engine.datasets import load_binance_candles, replay_candles  # noqa: PLC0415
    candles = load_binance_candles("btc_usdt_may2021")
    replay_candles(candles, pub, speed=speed, topo=topo, symbol="BTCUSDT")


# ── Process target ─────────────────────────────────────────────────────────────

def _feed_handler_target(mode: str = "gbm", speed: float = 1.0) -> None:
    """multiprocessing.Process target.

    zmq is imported HERE — after fork — to satisfy the "Context after fork"
    rule.  Raw ctx.socket() is banned; all sockets go through zmq_factory.
    """
    import zmq  # noqa: PLC0415 — intentional post-fork import

    topo = load_topology()
    ctx  = zmq.Context(io_threads=topo.zmq.io_threads)

    pub = zmq_factory.make_pub(ctx, topo)
    pub.bind(topo.get_bind_addr("feed_pub"))

    hb_pub = zmq_factory.make_pub(ctx, topo)
    hb_pub.bind(topo.get_bind_addr("heartbeat_pub"))

    # Slow-joiner guard: give subscribers time to connect before first tick
    time.sleep(topo.zmq.connect_sleep_ms / 1000.0)

    try:
        if mode == "gbm":
            _run_gbm_loop(pub, topo, hb_pub)
        elif mode == "stress":
            _run_stress_loop(pub, topo, hb_pub)
        elif mode == "replay":
            _run_replay_loop(pub, topo, speed)
        else:
            raise ValueError(f"Unknown feed mode: {mode!r}")
    finally:
        pub.close()
        hb_pub.close()
        ctx.term()


# ── Public API ─────────────────────────────────────────────────────────────────

def start_feed_handler(
    mode: str = "gbm",
    speed: float = 1.0,
) -> multiprocessing.Process:
    """Spawn and return a daemon Process running the feed handler.

    Parameters
    ----------
    mode  : "gbm" | "stress" | "replay"
    speed : playback speed multiplier (only meaningful for replay mode)

    Returns
    -------
    multiprocessing.Process — already started, daemon=True.
    """
    p = multiprocessing.Process(
        target=_feed_handler_target,
        args=(mode, speed),
        daemon=True,
    )
    p.start()
    return p
