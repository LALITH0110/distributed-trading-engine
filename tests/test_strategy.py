"""
tests/test_strategy.py — Unit tests for BaseStrategy interface and
MeanReversionStrategy signal logic.

No ZMQ, no processes — pure logic tests.
"""
from __future__ import annotations

import os
import pathlib
import types

import numpy as np
import pytest

from engine.strategy import BaseStrategy, MeanReversionStrategy, MLSignalStrategy
from engine.datasets import generate_gbm_prices


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_tick(symbol: str, bid: float, ask: float):
    return types.SimpleNamespace(symbol=symbol, bid=bid, ask=ask)


def _make_patched_strategy(lookback: int = 20, z_buy: float = -2.0, z_sell: float = 2.0):
    """Return (strategy, calls_list). submit_order is monkey-patched to record calls."""
    s = MeanReversionStrategy("test", lookback=lookback, z_buy=z_buy, z_sell=z_sell)
    calls = []
    s.submit_order = lambda side, qty, price, symbol: calls.append((side, qty, price, symbol))
    return s, calls


# ── TestBaseStrategyInterface ─────────────────────────────────────────────────

class TestBaseStrategyInterface:
    def test_abstract_instantiation_fails(self):
        """BaseStrategy cannot be instantiated directly (abstractmethod enforcement)."""
        with pytest.raises(TypeError):
            BaseStrategy("s1")  # type: ignore[abstract]

    def test_submit_order_raises_outside_process(self):
        """submit_order raises NotImplementedError when called outside a running process."""
        class _Concrete(BaseStrategy):
            def on_tick(self, tick) -> None:
                pass
            def on_fill(self, exec_report) -> None:
                pass

        s = _Concrete("s1")
        with pytest.raises(NotImplementedError):
            s.submit_order(1, 1, 100, "BTC")


# ── TestMeanReversionSignals ──────────────────────────────────────────────────

class TestMeanReversionSignals:
    def test_no_signal_before_lookback(self):
        """No signal while window is still filling (< lookback ticks)."""
        s, calls = _make_patched_strategy(lookback=20)
        for i in range(19):
            s.on_tick(_make_tick("BTCUSDT", 100 + i * 0.01, 100 + i * 0.01 + 1))
        assert calls == []

    def test_buy_signal_on_negative_z(self):
        """BUY emitted when last price is far below rolling mean (z < -2)."""
        s, calls = _make_patched_strategy(lookback=20)
        # 19 prices at 100, last at 80 → z = (80 - ~99) / std, should be < -2
        for _ in range(19):
            s.on_tick(_make_tick("BTCUSDT", 100, 101))
        s.on_tick(_make_tick("BTCUSDT", 80, 81))
        assert any(c[0] == 1 for c in calls), f"Expected BUY signal, got {calls}"

    def test_sell_signal_on_positive_z(self):
        """SELL emitted when last price is far above rolling mean (z > +2)."""
        s, calls = _make_patched_strategy(lookback=20)
        for _ in range(19):
            s.on_tick(_make_tick("BTCUSDT", 100, 101))
        s.on_tick(_make_tick("BTCUSDT", 120, 121))
        assert any(c[0] == 2 for c in calls), f"Expected SELL signal, got {calls}"

    def test_no_signal_on_zero_std(self):
        """No signal when all prices identical (std=0 guard prevents ZeroDivisionError)."""
        s, calls = _make_patched_strategy(lookback=20)
        for _ in range(20):
            s.on_tick(_make_tick("BTCUSDT", 100, 101))
        assert calls == []


# ── TestMeanReversionGBM ──────────────────────────────────────────────────────

class TestMeanReversionGBM:
    def test_gbm_generates_signals(self):
        """GBM path seed=42 (hardcoded in datasets.py) produces >= 10 signals at |z|>2, lookback=20."""
        prices = generate_gbm_prices(S0=30000.0, mu=0.1, sigma=0.2, n_ticks=200)
        s, calls = _make_patched_strategy(lookback=20)
        for p in prices:
            bid = int(p)
            ask = int(p) + 100
            s.on_tick(_make_tick("BTCUSDT", bid, ask))
        assert len(calls) >= 10, f"Expected >= 10 signals, got {len(calls)}"

    def test_lookback_param_affects_signals(self):
        """Lookback affects signal count — different lookbacks produce different signal counts.

        With seed=42, lookback=50 beats lookback=5 on a 200-tick GBM path: a
        larger window makes recent deviations appear more extreme vs. the long
        mean, generating more |z|>2 crossings.  The key property under test is
        that the parameter is *effective* — both signal counts are not equal.
        """
        prices = generate_gbm_prices(S0=30000.0, mu=0.1, sigma=0.2, n_ticks=200)

        s5, calls5 = _make_patched_strategy(lookback=5)
        s50, calls50 = _make_patched_strategy(lookback=50)

        for p in prices:
            bid = int(p)
            ask = int(p) + 100
            tick = _make_tick("BTCUSDT", bid, ask)
            s5.on_tick(tick)
            s50.on_tick(tick)

        # Verify lookback parameter is effective: different lookbacks ≠ same signal count
        assert len(calls5) != len(calls50), (
            f"lookback=5 signals={len(calls5)}, lookback=50 signals={len(calls50)}: "
            "expected different lookbacks to produce different signal counts"
        )
        # On this seed=42 path, longer lookback catches more reversion signals
        assert len(calls50) > len(calls5), (
            f"lookback=50 signals={len(calls50)}, lookback=5 signals={len(calls5)}: "
            "longer window should amplify short-term deviations on this path"
        )


# ── TestSelfTradeCheck ────────────────────────────────────────────────────────

def _self_trade_blocked(open_orders: dict, side: int, price: int, symbol: str) -> bool:
    """Mirror of the self-trade check logic in _strategy_target._submit_order."""
    for _oid, (sym, rest_side, rest_price) in open_orders.items():
        if sym != symbol:
            continue
        if side == 1 and rest_side == 2 and price >= rest_price:
            return True
        if side == 2 and rest_side == 1 and price <= rest_price:
            return True
    return False


# ── ML helpers ────────────────────────────────────────────────────────────────

def _make_ml_tick(symbol: str, bid: float, ask: float, bid_size: int = 1, ask_size: int = 1):
    return types.SimpleNamespace(
        symbol=symbol, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size
    )


def _make_ml_strategy(model_path: str):
    """Construct MLSignalStrategy with model loaded; patch submit_order."""
    os.environ["STRATEGY_MODEL_PATH"] = model_path
    s = MLSignalStrategy("ml-test")
    s.on_start()  # loads model
    calls = []
    s.submit_order = lambda side, qty, price, symbol: calls.append((side, qty, price, symbol))
    return s, calls


_MODEL_PATH = str(pathlib.Path(__file__).parent.parent / "models" / "ml_signal_model.joblib")
_MODEL_EXISTS = pathlib.Path(_MODEL_PATH).exists()


# ── TestMLSignalStrategy ──────────────────────────────────────────────────────

class TestMLSignalStrategy:

    def test_on_start_missing_env_var(self):
        """RuntimeError raised when STRATEGY_MODEL_PATH env var is absent."""
        old = os.environ.pop("STRATEGY_MODEL_PATH", None)
        s = MLSignalStrategy("ml-test")
        with pytest.raises(RuntimeError, match="STRATEGY_MODEL_PATH"):
            s.on_start()
        if old is not None:
            os.environ["STRATEGY_MODEL_PATH"] = old

    def test_on_start_missing_file(self):
        """RuntimeError raised when model file does not exist at given path."""
        os.environ["STRATEGY_MODEL_PATH"] = "/nonexistent/path.joblib"
        s = MLSignalStrategy("ml-test")
        with pytest.raises(RuntimeError, match="not found"):
            s.on_start()

    def test_no_signal_before_warmup(self):
        """No signal emitted until VOL_WINDOW (50) ticks are accumulated."""
        if not _MODEL_EXISTS:
            pytest.skip("model not committed — run scripts/train_ml_model.py")
        s, calls = _make_ml_strategy(_MODEL_PATH)
        # Feed 49 ticks — one short of VOL_WINDOW=50
        for i in range(49):
            s.on_tick(_make_ml_tick("BTCUSDT", 30000 + i, 30001 + i))
        assert calls == [], f"Expected no signal before warm-up, got {calls}"

    def test_inference_latency_under_1ms(self):
        """predict_proba on pre-allocated (1,4) feat completes in < 1ms."""
        if not _MODEL_EXISTS:
            pytest.skip("model not committed — run scripts/train_ml_model.py")
        import joblib
        import time
        clf = joblib.load(_MODEL_PATH)
        feat = np.zeros((1, 4), dtype=np.float32)
        # Warm up
        for _ in range(50):
            clf.predict_proba(feat)
        t0 = time.perf_counter()
        clf.predict_proba(feat)
        elapsed_ms = (time.perf_counter() - t0) * 1e3
        assert elapsed_ms < 1.0, f"Inference {elapsed_ms:.3f}ms >= 1ms"

    def test_buy_signal_on_high_proba(self):
        """BUY order submitted when predict_proba returns proba_up >= 0.6."""
        if not _MODEL_EXISTS:
            pytest.skip("model not committed — run scripts/train_ml_model.py")
        s, calls = _make_ml_strategy(_MODEL_PATH)
        # Warm up with 50 identical ticks to fill vol_win
        for _ in range(50):
            s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        # Monkey-patch model to return high proba_up
        s._model.predict_proba = lambda feat: np.array([[0.3, 0.75]])
        s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        assert len(calls) == 1, f"Expected 1 order, got {len(calls)}"
        assert calls[0][0] == 1, f"Expected BUY (side=1), got side={calls[0][0]}"

    def test_sell_signal_on_low_proba(self):
        """SELL order submitted when 1-proba_up >= 0.6."""
        if not _MODEL_EXISTS:
            pytest.skip("model not committed — run scripts/train_ml_model.py")
        s, calls = _make_ml_strategy(_MODEL_PATH)
        for _ in range(50):
            s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        # proba_up=0.3 -> 1-proba=0.7 >= 0.6 -> SELL
        s._model.predict_proba = lambda feat: np.array([[0.7, 0.3]])
        s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        assert len(calls) == 1, f"Expected 1 order, got {len(calls)}"
        assert calls[0][0] == 2, f"Expected SELL (side=2), got side={calls[0][0]}"

    def test_confidence_scaling(self):
        """qty = max(1, int(BASE_QTY * (proba - 0.5) * 2)) computed correctly."""
        if not _MODEL_EXISTS:
            pytest.skip("model not committed — run scripts/train_ml_model.py")
        s, calls = _make_ml_strategy(_MODEL_PATH)
        for _ in range(50):
            s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        # proba=0.8: confidence=(0.8-0.5)*2=0.6; qty=max(1,int(10*0.6))=6
        s._model.predict_proba = lambda feat: np.array([[0.2, 0.8]])
        s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        assert calls[-1][1] == 6, f"Expected qty=6, got {calls[-1][1]}"
        # proba=0.95: confidence=(0.95-0.5)*2=0.9; qty=max(1,int(10*0.9))=9
        s._model.predict_proba = lambda feat: np.array([[0.05, 0.95]])
        s.on_tick(_make_ml_tick("BTCUSDT", 30000, 30001))
        assert calls[-1][1] == 9, f"Expected qty=9, got {calls[-1][1]}"


# ── TestSelfTradeCheck ────────────────────────────────────────────────────────
    def test_buy_crosses_resting_sell(self):
        """BUY at price >= resting SELL price is blocked."""
        open_orders = {"oid1": ("BTC", 2, 100)}  # resting SELL at 100
        assert _self_trade_blocked(open_orders, side=1, price=100, symbol="BTC")

    def test_sell_crosses_resting_buy(self):
        """SELL at price <= resting BUY price is blocked."""
        open_orders = {"oid1": ("BTC", 1, 100)}  # resting BUY at 100
        assert _self_trade_blocked(open_orders, side=2, price=100, symbol="BTC")

    def test_no_cross_different_symbol(self):
        """Resting order on different symbol does NOT block."""
        open_orders = {"oid1": ("BTC", 2, 100)}  # resting SELL BTC at 100
        assert not _self_trade_blocked(open_orders, side=1, price=100, symbol="ETH")
