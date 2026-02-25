#!/usr/bin/env python3
"""
start_pipeline.py — Launch the full local trading pipeline for demo/observation.

Nodes started (in order):
  1. Matching Engine  (port 5559 PULL, 5556 PUB, 5562 PULL cancel)
  2. Risk Gateway     (port 5558 PULL, 5559 PUSH, 5561 PUB, 5560 PULL kill)
  3. Feed Handler     (port 5555 PUB, 5557 PUB heartbeat) — GBM mode
  4. Strategy 1       (MeanReversionStrategy "strat-001")
  5. Strategy 2       (MeanReversionStrategy "strat-002")

Press Ctrl+C to stop all nodes.
"""
import time
import sys

from engine.matching_engine import start_matching_engine
from engine.risk_gateway import start_risk_gateway
from engine.feed_handler import start_feed_handler
from engine.strategy import MeanReversionStrategy, start_strategy


def main() -> None:
    print("Starting Matching Engine...")
    me = start_matching_engine()
    time.sleep(0.3)

    print("Starting Risk Gateway...")
    rg = start_risk_gateway()
    time.sleep(0.3)

    print("Starting Feed Handler (GBM mode)...")
    fh = start_feed_handler(mode="gbm")
    time.sleep(0.5)

    print("Starting Strategy strat-001 (MeanReversion lookback=20)...")
    s1 = MeanReversionStrategy("strat-001", lookback=20)
    p1 = start_strategy(s1)

    print("Starting Strategy strat-002 (MeanReversion lookback=30)...")
    s2 = MeanReversionStrategy("strat-002", lookback=30)
    p2 = start_strategy(s2)

    print("\nAll nodes running. Press Ctrl+C to stop.\n")
    procs = [me, rg, fh, p1, p2]

    try:
        while True:
            alive = [p.is_alive() for p in procs]
            names = ["ME", "RG", "FH", "S1", "S2"]
            status = " | ".join(f"{n}={'UP' if a else 'DOWN'}" for n, a in zip(names, alive))
            print(f"\r{status}", end="", flush=True)
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for p in reversed(procs):
            if p.is_alive():
                p.terminate()
        for p in reversed(procs):
            p.join(timeout=2.0)
        print("All nodes stopped.")


if __name__ == "__main__":
    main()
