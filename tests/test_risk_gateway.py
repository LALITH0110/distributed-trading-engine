"""tests/test_risk_gateway.py — Unit tests for risk gateway filter logic. BENCH-05."""
import time
import types
import pytest

from engine.risk_gateway import check_fat_finger, check_position_limit, check_rate_limit, _build_reject_er


def _nos(price=50000, qty=1, side=1, strategy_id="S1", symbol="BTCUSDT", order_id="o1"):
    return types.SimpleNamespace(
        price=price, quantity=qty, side=side,
        strategy_id=strategy_id, symbol=symbol, order_id=order_id
    )


# ---------------------------------------------------------------------------
# TestFatFinger — RISK-01
# ---------------------------------------------------------------------------

class TestFatFinger:
    MAX = 100_000

    def test_pass_under_limit(self):
        """price=50000, qty=1 -> notional=50000 <= 100000: pass."""
        nos = _nos(price=50000, qty=1)
        assert check_fat_finger(nos, self.MAX) is True

    def test_pass_at_exact_limit(self):
        """price=50000, qty=2 -> notional=100000 == 100000 (boundary inclusive): pass."""
        nos = _nos(price=50000, qty=2)
        assert check_fat_finger(nos, self.MAX) is True

    def test_reject_over_limit(self):
        """price=200000, qty=1 -> notional=200000 > 100000: reject."""
        nos = _nos(price=200000, qty=1)
        assert check_fat_finger(nos, self.MAX) is False

    def test_reject_large_qty(self):
        """price=50000, qty=3 -> notional=150000 > 100000: reject."""
        nos = _nos(price=50000, qty=3)
        assert check_fat_finger(nos, self.MAX) is False


# ---------------------------------------------------------------------------
# TestPositionLimit — RISK-02
# ---------------------------------------------------------------------------

class TestPositionLimit:
    LIMIT = 10

    def test_buy_pass_empty(self):
        """BUY qty=5 on empty ledger -> 0+5=5 <= 10: pass."""
        positions = {}
        nos = _nos(qty=5, side=1)
        assert check_position_limit(nos, positions, self.LIMIT) is True

    def test_buy_pass_at_boundary(self):
        """BUY qty=10 on empty ledger -> 0+10=10 <= 10 (boundary inclusive): pass."""
        positions = {}
        nos = _nos(qty=10, side=1)
        assert check_position_limit(nos, positions, self.LIMIT) is True

    def test_buy_reject_over(self):
        """BUY qty=11 on empty ledger -> 0+11=11 > 10: reject."""
        positions = {}
        nos = _nos(qty=11, side=1)
        assert check_position_limit(nos, positions, self.LIMIT) is False

    def test_buy_reject_cumulative(self):
        """BUY qty=3 with positions[key]=8 -> 8+3=11 > 10: reject."""
        positions = {("S1", "BTCUSDT"): 8}
        nos = _nos(qty=3, side=1)
        assert check_position_limit(nos, positions, self.LIMIT) is False

    def test_sell_reject_breach(self):
        """SELL qty=5 with positions[key]=-6 -> -6-5=-11, abs=11 > 10: reject."""
        positions = {("S1", "BTCUSDT"): -6}
        nos = _nos(qty=5, side=2)
        assert check_position_limit(nos, positions, self.LIMIT) is False

    def test_sell_pass_boundary(self):
        """SELL qty=4 with positions[key]=-6 -> -6-4=-10, abs=10 <= 10: pass."""
        positions = {("S1", "BTCUSDT"): -6}
        nos = _nos(qty=4, side=2)
        assert check_position_limit(nos, positions, self.LIMIT) is True

    def test_independent_strategies(self):
        """Positions keyed by (strategy_id, symbol) — different strategies independent."""
        positions = {("S1", "BTCUSDT"): 9}
        # S2 has no position — should pass freely
        nos_s2 = _nos(qty=10, side=1, strategy_id="S2")
        assert check_position_limit(nos_s2, positions, self.LIMIT) is True
        # S1 with 9 already: BUY 2 -> 11 > 10: reject
        nos_s1 = _nos(qty=2, side=1, strategy_id="S1")
        assert check_position_limit(nos_s1, positions, self.LIMIT) is False


# ---------------------------------------------------------------------------
# TestRateLimiter — RISK-03
# ---------------------------------------------------------------------------

class TestRateLimiter:
    RATE = 10.0
    CAP = 10

    def test_full_bucket_passes(self):
        """10 orders at same timestamp -> all 10 pass (full bucket consumed)."""
        buckets = {}
        now = time.monotonic()
        nos = _nos()
        for _ in range(10):
            result = check_rate_limit(nos, buckets, self.RATE, self.CAP, now)
            assert result is True

    def test_11th_rejected(self):
        """11th order at same timestamp -> bucket empty: reject."""
        buckets = {}
        now = time.monotonic()
        nos = _nos()
        for _ in range(10):
            check_rate_limit(nos, buckets, self.RATE, self.CAP, now)
        # 11th call
        result = check_rate_limit(nos, buckets, self.RATE, self.CAP, now)
        assert result is False

    def test_refill_partial(self):
        """After draining bucket, 0.5s elapsed -> 5 tokens refilled; next 5 pass."""
        buckets = {}
        now = time.monotonic()
        nos = _nos()
        # Drain all 10
        for _ in range(10):
            check_rate_limit(nos, buckets, self.RATE, self.CAP, now)
        # Advance time by 0.5s -> 5 tokens refilled
        later = now + 0.5
        for _ in range(5):
            result = check_rate_limit(nos, buckets, self.RATE, self.CAP, later)
            assert result is True
        # 6th should fail (bucket empty again at same timestamp)
        result = check_rate_limit(nos, buckets, self.RATE, self.CAP, later)
        assert result is False

    def test_refill_full(self):
        """After draining, 1.0s elapsed -> full 10 tokens; next 10 calls pass."""
        buckets = {}
        now = time.monotonic()
        nos = _nos()
        # Drain all 10
        for _ in range(10):
            check_rate_limit(nos, buckets, self.RATE, self.CAP, now)
        # Advance time by 1.0s -> full refill (capped at capacity=10)
        later = now + 1.0
        for _ in range(10):
            result = check_rate_limit(nos, buckets, self.RATE, self.CAP, later)
            assert result is True
        # 11th should fail
        result = check_rate_limit(nos, buckets, self.RATE, self.CAP, later)
        assert result is False

    def test_independent_strategies(self):
        """Buckets keyed by strategy_id — draining S1 does not affect S2."""
        buckets = {}
        now = time.monotonic()
        nos_s1 = _nos(strategy_id="S1")
        nos_s2 = _nos(strategy_id="S2")
        # Drain S1 completely
        for _ in range(10):
            check_rate_limit(nos_s1, buckets, self.RATE, self.CAP, now)
        assert check_rate_limit(nos_s1, buckets, self.RATE, self.CAP, now) is False
        # S2 bucket untouched — first call should pass
        assert check_rate_limit(nos_s2, buckets, self.RATE, self.CAP, now) is True


# ---------------------------------------------------------------------------
# TestKillSwitchGate — validate _build_reject_er with KILL_SWITCH reason
# ---------------------------------------------------------------------------

class TestKillSwitchGate:
    """Validate _build_reject_er produces correct kill-switch reject ER."""

    def test_reject_er_exec_type(self):
        from proto.messages_pb2 import ExecutionReport, ExecType
        nos = _nos()
        raw = _build_reject_er(nos, "KILL_SWITCH")
        er = ExecutionReport()
        er.ParseFromString(raw)
        assert er.exec_type == ExecType.EXEC_TYPE_REJECT

    def test_reject_er_fields(self):
        from proto.messages_pb2 import ExecutionReport
        nos = _nos(strategy_id="STRAT-X", symbol="ETHUSDT", order_id="ord-99")
        raw = _build_reject_er(nos, "FAT_FINGER")
        er = ExecutionReport()
        er.ParseFromString(raw)
        assert er.strategy_id == "STRAT-X"
        assert er.symbol == "ETHUSDT"
        assert er.order_id == "ord-99"
        assert er.timestamp_ns > 0
        assert er.leaves_qty == nos.quantity


# ---------------------------------------------------------------------------
# TestRejectErRouting — RISK-05
# ---------------------------------------------------------------------------

class TestRejectErRouting:
    """RISK-05: reject ER carries correct strategy_id for PUB topic routing."""

    def test_strategy_id_preserved(self):
        from proto.messages_pb2 import ExecutionReport, ExecType
        nos = _nos(strategy_id="STRAT-42")
        raw = _build_reject_er(nos, "RATE_LIMIT")
        er = ExecutionReport()
        er.ParseFromString(raw)
        assert er.strategy_id == "STRAT-42"
        assert er.exec_type == ExecType.EXEC_TYPE_REJECT
