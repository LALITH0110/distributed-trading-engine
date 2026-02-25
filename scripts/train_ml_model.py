#!/usr/bin/env python3
"""
scripts/train_ml_model.py — Offline training for MLSignalStrategy. STRAT-05.

Usage:
    python scripts/train_ml_model.py

Reads:  data/btc_usdt_1yr.csv  (run fetch_binance_extended() first if absent)
Writes: models/ml_signal_model.joblib
        models/ml_model_meta.json

If data/btc_usdt_1yr.csv is absent, falls back in order:
  1. Calls fetch_binance_extended() (requires ccxt)
  2. Uses data/btc_usdt_may2021.csv if it exists
  3. Generates synthetic GBM candles as stand-in

Model: HistGradientBoostingClassifier, 70/30 chronological split, no data leakage.
Target: close[t+HORIZON] > close[t]  (HORIZON=10 ticks)
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

# ---------------------------------------------------------------------------
# Bootstrap project root onto path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from engine.datasets import DATA_DIR, load_binance_candles  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODELS_DIR = Path(__file__).parent.parent / "models"
FEATURE_ORDER = ["volatility", "spread_ratio", "volume_imbalance", "momentum"]
VOL_WINDOW = 50
MOM_WINDOW = 20
VI_WINDOW = 20
HORIZON = 10


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_features(candles: list) -> tuple[np.ndarray, np.ndarray]:
    """Extract feature matrix X and label vector y from raw candles.

    Parameters
    ----------
    candles : list of [timestamp_ms, open, high, low, close, volume]

    Returns
    -------
    X : np.ndarray of shape (n_samples, 4), dtype=float32
        Columns: volatility, spread_ratio, volume_imbalance, momentum
    y : np.ndarray of shape (n_samples,), dtype=int32
        Binary label: 1 if closes[i+HORIZON] > closes[i], else 0

    Notes
    -----
    Training loop covers candles[:-HORIZON] so label lookahead never
    references a future row beyond the dataset boundary.
    No shuffle — chronological order preserved.
    """
    closes = [c[4] for c in candles]
    vols = [c[5] for c in candles]
    opens = [c[1] for c in candles]

    vol_win: deque = deque(maxlen=VOL_WINDOW)
    mom_win: deque = deque(maxlen=MOM_WINDOW)
    vi_win: deque = deque(maxlen=VI_WINDOW)

    rows: list = []
    labels: list = []

    for i in range(len(candles) - HORIZON):
        close = closes[i]
        open_ = opens[i]
        volume = vols[i]

        vol_win.append(close)
        mom_win.append(close)

        buy_vol = volume if close >= open_ else 0.0
        sell_vol = volume if close < open_ else 0.0
        vi_win.append((buy_vol, sell_vol))

        if len(vol_win) < VOL_WINDOW:
            continue

        # Feature 0: volatility
        volatility = float(np.std(np.array(vol_win, dtype=np.float64)))

        # Feature 1: spread_ratio (open/close proxy — single tick)
        spread_ratio = abs(close - open_) / close if close > 0 else 0.0

        # Feature 2: volume_imbalance
        bv = sum(x[0] for x in vi_win)
        sv = sum(x[1] for x in vi_win)
        vi = (bv - sv) / (bv + sv) if (bv + sv) > 0 else 0.0

        # Feature 3: momentum
        if len(mom_win) < MOM_WINDOW:
            continue
        prices = list(mom_win)
        momentum = (prices[-1] - prices[0]) / prices[0] if prices[0] != 0 else 0.0

        rows.append([volatility, spread_ratio, vi, momentum])
        labels.append(1 if closes[i + HORIZON] > closes[i] else 0)

    return np.array(rows, dtype=np.float32), np.array(labels, dtype=np.int32)


# ---------------------------------------------------------------------------
# Synthetic candle generator (fallback)
# ---------------------------------------------------------------------------

def _generate_synthetic_candles(n: int = 60_000) -> list:
    """Generate synthetic BTC/USDT-like 1m candles via GBM for training stand-in.

    Parameters
    ----------
    n : number of candles to generate (default 60_000 ~ ~42 days of 1m bars)

    Returns
    -------
    list of [timestamp_ms, open, high, low, close, volume]
    """
    from engine.datasets import generate_gbm_prices

    prices = generate_gbm_prices(
        S0=30_000.0, mu=0.40, sigma=0.80, n_ticks=n
    )
    rng = np.random.default_rng(seed=42)
    volumes = rng.uniform(100.0, 2000.0, n)

    # Base timestamp: 2020-01-01 00:00:00 UTC in ms
    base_ms = int(
        datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    )
    candles = []
    for i, (p, v) in enumerate(zip(prices, volumes)):
        ts = base_ms + i * 60_000  # 1-minute bars
        noise = rng.uniform(-0.002, 0.002)
        open_ = float(p * (1 + noise))
        high = float(p * (1 + abs(noise) + rng.uniform(0, 0.002)))
        low = float(p * (1 - abs(noise) - rng.uniform(0, 0.002)))
        close = float(p)
        candles.append([ts, open_, high, low, close, float(v)])
    return candles


# ---------------------------------------------------------------------------
# Main training entrypoint
# ---------------------------------------------------------------------------

def train_and_save(csv_name: str = "btc_usdt_1yr") -> None:
    """Load data, engineer features, train HGB classifier, save artifacts.

    Parameters
    ----------
    csv_name : CSV stem to load from data/; default "btc_usdt_1yr"

    Side effects
    ------------
    Writes models/ml_signal_model.joblib
    Writes models/ml_model_meta.json
    """
    MODELS_DIR.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Load candles — with fallback chain
    # ------------------------------------------------------------------
    candles = None
    data_source = csv_name

    # Try primary CSV
    primary_path = DATA_DIR / f"{csv_name}.csv"
    if primary_path.exists():
        print(f"Loading {primary_path} …")
        candles = load_binance_candles(csv_name)
    else:
        # Try fetching from Binance
        try:
            from scripts.fetch_datasets import fetch_binance_extended  # noqa: PLC0415
            print("data/btc_usdt_1yr.csv not found. Fetching from Binance (~10 min) …")
            fetch_binance_extended(csv_name=csv_name)
            candles = load_binance_candles(csv_name)
        except Exception as exc:
            print(f"Binance fetch failed ({exc}). Trying fallback CSV …")

        # Try btc_usdt_may2021 fallback
        if candles is None:
            may_path = DATA_DIR / "btc_usdt_may2021.csv"
            if may_path.exists():
                print("Using fallback: data/btc_usdt_may2021.csv")
                candles = load_binance_candles("btc_usdt_may2021")
                data_source = "btc_usdt_may2021"

        # Last resort: synthetic GBM
        if candles is None:
            print("No CSV available. Generating synthetic GBM candles (60,000 rows) …")
            candles = _generate_synthetic_candles(60_000)
            data_source = "synthetic_gbm_60k"

    print(f"Loaded {len(candles):,} candles from '{data_source}'")

    # ------------------------------------------------------------------
    # 2. Feature engineering
    # ------------------------------------------------------------------
    print("Engineering features …")
    t0 = time.perf_counter()
    X, y = build_features(candles)
    elapsed = time.perf_counter() - t0
    print(f"  X shape: {X.shape}, y shape: {y.shape}  ({elapsed:.2f}s)")

    if len(X) == 0:
        raise RuntimeError("build_features returned 0 rows — dataset too short")

    # ------------------------------------------------------------------
    # 3. Chronological 70/30 split (no shuffle — prevents data leakage)
    # ------------------------------------------------------------------
    split = int(len(X) * 0.7)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    print(f"  Train: {len(X_train):,}  Test: {len(X_test):,}")

    # ------------------------------------------------------------------
    # 4. Train
    # ------------------------------------------------------------------
    print("Training HistGradientBoostingClassifier …")
    clf = HistGradientBoostingClassifier(
        max_iter=10,
        max_leaf_nodes=15,
        early_stopping=False,
        random_state=42,
    )
    clf.fit(X_train, y_train)

    train_acc = float(clf.score(X_train, y_train))
    test_acc = float(clf.score(X_test, y_test))
    print(f"  train_accuracy: {train_acc:.4f}  test_accuracy: {test_acc:.4f}")

    # ------------------------------------------------------------------
    # 5. Save model
    # ------------------------------------------------------------------
    model_path = MODELS_DIR / "ml_signal_model.joblib"
    joblib.dump(clf, model_path)
    model_kb = model_path.stat().st_size / 1024
    print(f"  Saved model: {model_path}  ({model_kb:.1f} KB)")

    # ------------------------------------------------------------------
    # 6. Derive metadata timestamps
    # ------------------------------------------------------------------
    last_ts_ms = candles[-1][0]
    first_ts_ms = candles[0][0]
    training_cutoff = datetime.fromtimestamp(
        last_ts_ms / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    dataset_start = datetime.fromtimestamp(
        first_ts_ms / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # 7. Write meta JSON
    # ------------------------------------------------------------------
    meta = {
        "training_cutoff": training_cutoff,
        "dataset_range": {
            "start": dataset_start,
            "end": training_cutoff,
        },
        "dataset_rows": len(candles),
        "data_source": data_source,
        "feature_windows": {
            "volatility_ticks": 50,
            "momentum_ticks": 20,
            "volume_imbalance_ticks": 20,
            "spread_ratio": "single_tick_open_close_proxy",
        },
        "feature_order": FEATURE_ORDER,
        "model": {
            "type": "HistGradientBoostingClassifier",
            "max_iter": 10,
            "max_leaf_nodes": 15,
            "early_stopping": False,
            "random_state": 42,
        },
        "train_accuracy": round(train_acc, 4),
        "test_accuracy": round(test_acc, 4),
        "split": "70/30_chronological",
        "target": "close[t+10] > close[t]",
        "sklearn_version": "1.6.1",
        "notes": (
            "volume_imbalance uses buy/sell split inferred from close>open; "
            "bid_size==ask_size in live feed so this feature carries no live signal"
        ),
    }

    meta_path = MODELS_DIR / "ml_model_meta.json"
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"  Saved meta:  {meta_path}")

    print("\nTraining complete.")
    print(f"  model: {model_path}")
    print(f"  meta:  {meta_path}")
    print(f"  train_acc={train_acc:.4f}  test_acc={test_acc:.4f}")


if __name__ == "__main__":
    train_and_save()
