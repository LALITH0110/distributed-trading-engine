#!/usr/bin/env python3
"""
scripts/fetch_datasets.py — one-time dataset fetch.
Run once before tests that use historical data:
    python scripts/fetch_datasets.py

Writes to:
    data/btc_usdt_may2021.csv     — Binance BTC/USDT 1m candles, May 2021
    data/btc_usdt_1yr.csv         — Binance BTC/USDT 1m candles, 2020-01-01 to 2021-01-01
    data/aapl_history.csv         — Yahoo Finance AAPL daily, Jan-Dec 2021
    data/msft_history.csv         — Yahoo Finance MSFT daily, Jan-Dec 2021
    data/spy_history.csv          — Yahoo Finance SPY daily, Jan-Dec 2021

Requires:
    pip install ccxt yfinance

Extended fetch (fetch_binance_extended) produces 1-year BTC/USDT 1m dataset for
ML model training. Call it directly or via scripts/train_ml_model.py.
"""

import sys
import time
from pathlib import Path

# Ensure project root is on the path so engine.datasets is importable
# when the script is run from any working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from engine.datasets import _write_candles_csv, DATA_DIR  # noqa: E402


# ---------------------------------------------------------------------------
# Binance — BTC/USDT 1m candles, May 2021
# ---------------------------------------------------------------------------

def fetch_binance() -> None:
    """Fetch BTC/USDT 1m candles for May 2021 from Binance via ccxt.

    Uses paginated fetch_ohlcv with enableRateLimit=True. Writes result to
    data/btc_usdt_may2021.csv via engine.datasets._write_candles_csv.
    """
    import ccxt  # noqa: PLC0415 — network dep; not imported at module level

    ex = ccxt.binance({"enableRateLimit": True})

    since_ms: int = ex.parse8601("2021-05-01 00:00:00")
    until_ms: int = ex.parse8601("2021-05-31 23:59:00")

    all_candles: list = []
    while True:
        batch = ex.fetch_ohlcv("BTC/USDT", "1m", since=since_ms, limit=1000)
        if not batch:
            break
        all_candles.extend(batch)
        last_ts: int = batch[-1][0]
        if last_ts >= until_ms or len(batch) < 1000:
            break
        since_ms = last_ts + 1
        time.sleep(ex.rateLimit / 1000)

    candles = [c for c in all_candles if c[0] <= until_ms]
    out_path = DATA_DIR / "btc_usdt_may2021.csv"
    _write_candles_csv(out_path, candles)
    print(f"Fetched {len(candles)} BTC/USDT candles → {out_path}")


# ---------------------------------------------------------------------------
# Binance — BTC/USDT 1m candles, extended range (for ML training)
# ---------------------------------------------------------------------------

def fetch_binance_extended(
    start_date: str = "2020-01-01",
    end_date: str = "2021-01-01",
    csv_name: str = "btc_usdt_1yr",
) -> None:
    """Fetch BTC/USDT 1m candles for a full year from Binance via ccxt.

    Mirrors fetch_binance() pagination pattern exactly. Writes result to
    data/{csv_name}.csv via engine.datasets._write_candles_csv.

    Parameters
    ----------
    start_date : ISO date string (inclusive), default "2020-01-01"
    end_date   : ISO date string (inclusive), default "2021-01-01"
    csv_name   : filename stem (no extension), default "btc_usdt_1yr"

    Notes
    -----
    Fetches up to ~525,600 candles (1 year × 60 min × 24 h). Expect ~10-15 min
    runtime due to Binance rate limits. Used by scripts/train_ml_model.py.
    """
    import ccxt  # noqa: PLC0415 — network dep; not imported at module level

    ex = ccxt.binance({"enableRateLimit": True})

    since_ms: int = ex.parse8601(f"{start_date} 00:00:00")
    until_ms: int = ex.parse8601(f"{end_date} 23:59:00")

    all_candles: list = []
    while True:
        batch = ex.fetch_ohlcv("BTC/USDT", "1m", since=since_ms, limit=1000)
        if not batch:
            break
        all_candles.extend(batch)
        last_ts: int = batch[-1][0]
        if last_ts >= until_ms or len(batch) < 1000:
            break
        since_ms = last_ts + 1
        time.sleep(ex.rateLimit / 1000)

    candles = [c for c in all_candles if c[0] <= until_ms]
    _write_candles_csv(DATA_DIR / f"{csv_name}.csv", candles)
    print(
        f"Fetched {len(candles)} BTC/USDT candles"
        f" ({start_date} to {end_date}) -> data/{csv_name}.csv"
    )


# ---------------------------------------------------------------------------
# Yahoo Finance — AAPL, MSFT, SPY daily candles, Jan-Dec 2021
# ---------------------------------------------------------------------------

def fetch_yahoo(
    symbol: str,
    start: str = "2021-01-01",
    end: str = "2021-12-31",
) -> None:
    """Fetch daily OHLCV for `symbol` from Yahoo Finance via yfinance.

    Uses yf.Ticker.history() — NOT yf.download() — to avoid MultiIndex pitfall.
    Writes result to data/{symbol.lower()}_history.csv.

    Parameters
    ----------
    symbol : ticker symbol (e.g. "AAPL", "MSFT", "SPY")
    start  : ISO date string, inclusive
    end    : ISO date string, inclusive
    """
    import yfinance as yf  # noqa: PLC0415 — network dep; not imported at module level

    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end, interval="1d")

    candles = [
        [
            int(row.Index.timestamp() * 1000),
            row.Open,
            row.High,
            row.Low,
            row.Close,
            row.Volume,
        ]
        for row in df.itertuples()
    ]

    out_path = DATA_DIR / f"{symbol.lower()}_history.csv"
    _write_candles_csv(out_path, candles)
    print(f"Fetched {len(df)} {symbol} candles → {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Fetch all datasets. Safe to re-run — overwrites existing CSVs."""
    print("Fetching Binance BTC/USDT May 2021 (this may take ~2-3 min) …")
    fetch_binance()

    for sym in ("AAPL", "MSFT", "SPY"):
        print(f"Fetching Yahoo Finance {sym} 2021 …")
        fetch_yahoo(sym)

    print("All datasets fetched to data/")


if __name__ == "__main__":
    main()
