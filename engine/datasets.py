"""
engine/datasets.py — Data layer: GBM generator, CSV-cached candle loaders,
wall-clock replay emitter.

Public API:
    generate_gbm_prices(S0, mu, sigma, n_ticks, dt) -> np.ndarray
    load_binance_candles(csv_name) -> list[list]
    load_yahoo_candles(symbol, csv_suffix) -> list[list]
    replay_candles(candles, pub, speed, topo, symbol) -> None

Data directory (never committed — see .gitignore):
    data/btc_usdt_may2021.csv   — Binance BTC/USDT 1m candles, May 2021
    data/aapl_history.csv       — Yahoo Finance AAPL daily, Jan-Dec 2021
    data/msft_history.csv       — Yahoo Finance MSFT daily, Jan-Dec 2021
    data/spy_history.csv        — Yahoo Finance SPY daily, Jan-Dec 2021

CSV schema (all files): timestamp_ms, open, high, low, close, volume
Timestamps are milliseconds from ccxt/Yahoo. Multiply by 1_000_000 for nanoseconds.
"""

import csv
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR: Path = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# 1. GBM synthetic price generator
# ---------------------------------------------------------------------------

def generate_gbm_prices(
    S0: float,
    mu: float,
    sigma: float,
    n_ticks: int,
    dt: float = 1 / 252 / 6.5 / 3600,
) -> np.ndarray:
    """Return a reproducible geometric Brownian motion price path.

    Parameters
    ----------
    S0      : initial price
    mu      : annualised drift (e.g. 0.40 = 40 %)
    sigma   : annualised volatility (e.g. 0.30 = 30 %)
    n_ticks : number of price ticks to generate
    dt      : time step in years (default: 1 second of a 6.5-hour trading day)

    Returns
    -------
    np.ndarray of shape (n_ticks,) — float64 prices

    Notes
    -----
    Seed is always 42 for full reproducibility across calls.
    No random state is modified outside this function.
    """
    rng = np.random.default_rng(seed=42)
    increments = np.exp(
        (mu - 0.5 * sigma ** 2) * dt
        + sigma * np.sqrt(dt) * rng.standard_normal(n_ticks)
    )
    return S0 * np.cumprod(increments)


# ---------------------------------------------------------------------------
# 2. CSV-cached candle loaders
# ---------------------------------------------------------------------------

def load_binance_candles(csv_name: str = "btc_usdt_may2021") -> list:
    """Load Binance candles from CSV cache.

    Parameters
    ----------
    csv_name : filename stem (no extension); default = btc_usdt_may2021

    Returns
    -------
    list of [timestamp_ms: int, open: float, high: float, low: float,
             close: float, volume: float]

    Raises
    ------
    FileNotFoundError if the CSV has not been fetched yet.
        Run ``python scripts/fetch_datasets.py`` to populate data/.

    Notes
    -----
    Timestamps are milliseconds (ccxt convention). Multiply by 1_000_000
    to get nanoseconds for MarketDataTick.timestamp_ns.
    """
    path = DATA_DIR / f"{csv_name}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset not fetched. Run: python scripts/fetch_datasets.py\n"
            f"Expected: {path}"
        )
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        next(reader)  # skip header
        return [[int(row[0])] + [float(x) for x in row[1:]] for row in reader]


def load_yahoo_candles(symbol: str, csv_suffix: str = "_history") -> list:
    """Load Yahoo Finance candles from CSV cache.

    Parameters
    ----------
    symbol     : ticker symbol (case-insensitive); e.g. "AAPL", "MSFT", "SPY"
    csv_suffix : filename suffix before .csv; default = _history

    Returns
    -------
    list of [timestamp_ms: int, open: float, high: float, low: float,
             close: float, volume: float]

    Raises
    ------
    FileNotFoundError if the CSV has not been fetched yet.
        Run ``python scripts/fetch_datasets.py`` to populate data/.
    """
    path = DATA_DIR / f"{symbol.lower()}{csv_suffix}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset not fetched. Run: python scripts/fetch_datasets.py\n"
            f"Expected: {path}"
        )
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        next(reader)  # skip header
        return [[int(row[0])] + [float(x) for x in row[1:]] for row in reader]


# ---------------------------------------------------------------------------
# 3. Wall-clock replay emitter
# ---------------------------------------------------------------------------

def replay_candles(
    candles: list,
    pub,
    speed: float,
    topo,
    symbol: str = "BTCUSDT",
) -> None:
    """Emit candles at wall-clock rate via a ZeroMQ PUB socket.

    Uses absolute monotonic targets — NOT relative sleeps — to avoid drift
    accumulation over long replays.

    Parameters
    ----------
    candles : list of [timestamp_ms, open, high, low, close, volume]
    pub     : zmq.Socket (PUB) — must already be bound by caller
    speed   : replay multiplier; 1.0 = real-time, 10.0 = 10x faster
    topo    : engine.config.Topology (unused directly; reserved for future use)
    symbol  : tick symbol written to MarketDataTick.symbol

    Protocol
    --------
    Each candle is emitted as a MarketDataTick protobuf:
        schema_version = 1
        timestamp_ns   = time.time_ns()          (wall clock at emit)
        symbol         = symbol param
        bid            = int(open  * 100)        (int64 ticks, 2 dp)
        ask            = int(close * 100)        (int64 ticks, 2 dp)
        bid_size       = int(volume)
        ask_size       = int(volume)

    Timing
    ------
    wall_start_ns  = time.monotonic_ns() at first candle
    data_start_ms  = candles[0][0]
    For each candle:
        data_offset_ns  = (candle[0] - data_start_ms) * 1_000_000
        target_wall_ns  = wall_start_ns + int(data_offset_ns / speed)
        if target_wall_ns > now: sleep remainder
    """
    # Deferred import: avoids circular-import issues at module load time.
    from proto.messages_pb2 import MarketDataTick  # noqa: PLC0415

    tick = MarketDataTick()  # reuse ONE object — no per-tick heap allocation
    tick.schema_version = 1

    wall_start_ns: int = time.monotonic_ns()
    data_start_ms: int = candles[0][0]

    for candle in candles:
        # Absolute wall-clock target (avoids drift)
        data_offset_ns = (candle[0] - data_start_ms) * 1_000_000
        target_wall_ns = wall_start_ns + int(data_offset_ns / speed)
        now = time.monotonic_ns()
        if target_wall_ns > now:
            time.sleep((target_wall_ns - now) / 1e9)

        # Populate tick (reusing same object)
        tick.timestamp_ns = time.time_ns()
        tick.symbol = symbol
        tick.bid = int(candle[1] * 100)      # open price
        tick.ask = int(candle[4] * 100)      # close price
        tick.bid_size = int(candle[5])       # volume
        tick.ask_size = int(candle[5])

        pub.send(tick.SerializeToString())


# ---------------------------------------------------------------------------
# 4. Private helper — CSV writer
# ---------------------------------------------------------------------------

def _write_candles_csv(path: Path, candles: list) -> None:
    """Write candles list to CSV with standard header.

    Creates DATA_DIR if it does not exist. Used by scripts/fetch_datasets.py.

    Parameters
    ----------
    path    : full output path (e.g. DATA_DIR / "btc_usdt_may2021.csv")
    candles : list of [timestamp_ms, open, high, low, close, volume]
    """
    DATA_DIR.mkdir(exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp_ms", "open", "high", "low", "close", "volume"])
        writer.writerows(candles)
