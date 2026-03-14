#!/usr/bin/env python3
"""
start_pipeline.py — Launch the full local trading pipeline.

Nodes started (in order):
  1. Matching Engine  (PULL 5559, PUB 5556, PULL 5562 cancel, PUB 5563 hb)
  2. Risk Gateway     (PULL 5558, PUSH 5559, PUB 5561, PULL 5560, PUB 5564 hb)
  3. Feed Handler     (PUB 5555, PUB 5557 heartbeat) — GBM mode
  4. Strategy 1       (MeanReversionStrategy "strat-001", pure buyer)
  5. Strategy 2       (MeanReversionStrategy "strat-002", pure seller)

Usage:
  python scripts/start_pipeline.py              # run until Ctrl+C
  python scripts/start_pipeline.py --duration 60  # run for 60s then exit

Press Ctrl+C to stop all nodes.
"""
import argparse
import os
import sys
import time

from engine.matching_engine import start_matching_engine
from engine.risk_gateway import start_risk_gateway
from engine.feed_handler import start_feed_handler
from engine.strategy import MeanReversionStrategy, MLSignalStrategy, start_strategy


def _wait_alive(procs: list, names: list[str], timeout_s: float = 5.0) -> bool:
    """Wait until all processes are alive or timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(p.is_alive() for p in procs):
            return True
        time.sleep(0.1)
    dead = [n for n, p in zip(names, procs) if not p.is_alive()]
    print(f"FAILED to start: {', '.join(dead)}", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch local trading pipeline")
    parser.add_argument("--duration", type=int, default=0,
                        help="Run for N seconds then exit (0 = run until Ctrl+C)")
    parser.add_argument("--mode", choices=["gbm", "replay", "stress"], default="gbm",
                        help="Feed handler mode (default: gbm)")
    parser.add_argument("--with-ml", action="store_true",
                        help="Include MLSignalStrategy node")
    args = parser.parse_args()

    procs = []
    names = []

    # 1. Matching Engine
    print("[1/5] Matching Engine...", end=" ", flush=True)
    me = start_matching_engine()
    procs.append(me); names.append("ME")
    time.sleep(0.3)
    print("UP" if me.is_alive() else "FAIL")

    # 2. Risk Gateway
    print("[2/5] Risk Gateway...", end=" ", flush=True)
    rg = start_risk_gateway()
    procs.append(rg); names.append("RG")
    time.sleep(0.3)
    print("UP" if rg.is_alive() else "FAIL")

    # 3. Feed Handler
    print(f"[3/5] Feed Handler ({args.mode})...", end=" ", flush=True)
    fh = start_feed_handler(mode=args.mode)
    procs.append(fh); names.append("FH")
    time.sleep(0.5)
    print("UP" if fh.is_alive() else "FAIL")

    # 4. Strategy strat-001 (pure buyer: z_sell=99 never triggers sells)
    print("[4/5] strat-001 (MR buyer)...", end=" ", flush=True)
    s1 = MeanReversionStrategy("strat-001", lookback=20, z_buy=-2.0, z_sell=99.0)
    p1 = start_strategy(s1)
    procs.append(p1); names.append("S1")
    print("UP" if p1.is_alive() else "FAIL")

    # 5. Strategy strat-002 (pure seller: z_buy=-99 never triggers buys)
    print("[5/5] strat-002 (MR seller)...", end=" ", flush=True)
    s2 = MeanReversionStrategy("strat-002", lookback=20, z_buy=-99.0, z_sell=2.0)
    p2 = start_strategy(s2)
    procs.append(p2); names.append("S2")
    print("UP" if p2.is_alive() else "FAIL")

    # Optional: ML strategy
    if args.with_ml:
        model_path = os.environ.get("STRATEGY_MODEL_PATH",
                                     str(os.path.join(os.path.dirname(__file__), "..", "models", "ml_signal_model.joblib")))
        os.environ.setdefault("STRATEGY_MODEL_PATH", model_path)
        print("[+] ml-strat-001 (ML signal)...", end=" ", flush=True)
        s3 = MLSignalStrategy("ml-strat-001")
        p3 = start_strategy(s3)
        procs.append(p3); names.append("ML")
        print("UP" if p3.is_alive() else "FAIL")

    # Startup sync — verify all alive
    if not _wait_alive(procs, names):
        for p in reversed(procs):
            if p.is_alive():
                p.terminate()
        sys.exit(1)

    print(f"\nAll {len(procs)} nodes running.", flush=True)
    if args.duration > 0:
        print(f"Running for {args.duration}s...")

    start_time = time.monotonic()
    try:
        while True:
            alive = [p.is_alive() for p in procs]
            status = " | ".join(f"{n}={'UP' if a else 'DOWN'}" for n, a in zip(names, alive))
            elapsed = int(time.monotonic() - start_time)
            print(f"\r[{elapsed:4d}s] {status}", end="", flush=True)

            if not all(alive):
                dead = [n for n, a in zip(names, alive) if not a]
                print(f"\nNode(s) died: {', '.join(dead)}")
                break

            if args.duration > 0 and elapsed >= args.duration:
                print(f"\nDuration {args.duration}s reached.")
                break

            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\nCtrl+C received.")
    finally:
        print("Shutting down...", end=" ", flush=True)
        for p in reversed(procs):
            if p.is_alive():
                p.terminate()
        for p in reversed(procs):
            p.join(timeout=2.0)
        print("All nodes stopped.")


if __name__ == "__main__":
    main()
