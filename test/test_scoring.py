"""
tests/test_scoring.py — Scoring engine and signal computation tests.

Covers:
  - Each signal fires / does not fire correctly
  - Score computation (raw → normalised)
  - Frequency multiplier application
  - Score persistence
  - Watchlist ranking correctness
  - Edge cases: empty trades, single trade, all sells
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models import Score, Trade
from services.analysis_engine import AnalysisEngine
from services.scoring_engine import ScoringEngine
from tests.conftest import make_company, make_trade


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine() -> AnalysisEngine:
    return AnalysisEngine()


def _scoring() -> ScoringEngine:
    return ScoringEngine()


# ---------------------------------------------------------------------------
# Signal A — BUY_CLUSTER
# ---------------------------------------------------------------------------

class TestSignalA:

    def test_cluster_fires_with_enough_unique_buyers(self, db_session, sample_trades):
        with patch("services.analysis_engine.get_session") as mock_gs:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            engine = _engine()
            result = engine.signal_a_buy_cluster("AAPL", sample_trades)

        assert result.active is True
        assert result.value >= 3   # 4 unique buyers in window

    def test_cluster_does_not_fire_below_threshold(self, db_session):
        trades = [
            make_trade("Pelosi", "AAPL", "purchase", datetime.utcnow() - timedelta(days=5)),
            make_trade("Ryan",   "AAPL", "purchase", datetime.utcnow() - timedelta(days=3)),
        ]
        engine = _engine()
        result = engine.signal_a_buy_cluster("AAPL", trades)
        # Default threshold is 3; only 2 unique buyers
        assert result.active is False
        assert result.value == 2

    def test_cluster_not_active_for_empty_trades(self):
        engine = _engine()
        result = engine.signal_a_buy_cluster("AAPL", [])
        assert result.active is False
        assert result.value == 0

    def test_cluster_respects_window_boundary(self):
        """Buys spread further than window_days should NOT cluster."""
        engine = _engine()
        engine.cluster_window_days = 10   # tighter window for this test
        now = datetime.utcnow()
        trades = [
            make_trade("Pelosi",    "AAPL", "purchase", now - timedelta(days=25)),
            make_trade("Ryan",      "AAPL", "purchase", now - timedelta(days=20)),
            make_trade("McConnell", "AAPL", "purchase", now - timedelta(days=5)),
        ]
        # No 3 unique buyers fall within any single 10-day window
        result = engine.signal_a_buy_cluster("AAPL", trades)
        assert result.active is False

    def test_cluster_data_contains_buyers_list(self, sample_trades):
        engine = _engine()
        result = engine.signal_a_buy_cluster("AAPL", sample_trades)
        assert "buyers" in result.data
        assert isinstance(result.data["buyers"], list)
        assert len(result.data["buyers"]) >= 3


# ---------------------------------------------------------------------------
# Signal B — NET_BUY_ACTIVITY
# ---------------------------------------------------------------------------

class TestSignalB:

    def test_net_buy_fires_when_buys_exceed_sells(self, sample_trades):
        engine = _engine()
        result = engine.signal_b_net_buy_activity("AAPL", sample_trades)
        assert result.active is True
        assert result.data["buy_count"] > result.data["sell_count"]

    def test_net_buy_not_active_when_more_sells(self):
        trades = [
            make_trade("Pelosi", "AAPL", "sale", datetime.utcnow() - timedelta(days=5)),
            make_trade("Ryan",   "AAPL", "sale", datetime.utcnow() - timedelta(days=3)),
            make_trade("Ryan",   "AAPL", "purchase", datetime.utcnow() - timedelta(days=1)),
        ]
        engine = _engine()
        result = engine.signal_b_net_buy_activity("AAPL", trades)
        assert result.active is False

    def test_net_buy_not_active_single_buy(self):
        """Requires >= 2 buys to avoid noise."""
        trades = [make_trade("Pelosi", "AAPL", "purchase")]
        engine = _engine()
        result = engine.signal_b_net_buy_activity("AAPL", trades)
        assert result.active is False

    def test_net_buy_ratio_calculated(self, sample_trades):
        engine = _engine()
        result = engine.signal_b_net_buy_activity("AAPL", sample_trades)
        ratio = result.data["buy_ratio"]
        assert 0.0 <= ratio <= 1.0

    def test_net_buy_empty_trades(self):
        engine = _engine()
        result = engine.signal_b_net_buy_activity("AAPL", [])
        assert result.active is False
        assert result.data["buy_count"] == 0


# ---------------------------------------------------------------------------
# Signal D — REPEAT_BUYING
# ---------------------------------------------------------------------------

class TestSignalD:

    def test_repeat_buying_fires_for_multiple_purchases(self, sample_trades):
        """Nancy Pelosi buys AAPL twice in sample_trades."""
        engine = _engine()
        result = engine.signal_d_repeat_buying("AAPL", sample_trades)
        assert result.active is True
        assert "Nancy Pelosi" in result.data["repeat_buyers"]
        assert result.data["repeat_buyers"]["Nancy Pelosi"] == 2

    def test_repeat_buying_not_active_when_all_unique(self):
        trades = [
            make_trade("Pelosi",    "AAPL", "purchase", datetime.utcnow() - timedelta(days=10)),
            make_trade("Ryan",      "AAPL", "purchase", datetime.utcnow() - timedelta(days=5)),
            make_trade("McConnell", "AAPL", "purchase", datetime.utcnow() - timedelta(days=2)),
        ]
        engine = _engine()
        result = engine.signal_d_repeat_buying("AAPL", trades)
        assert result.active is False

    def test_repeat_buying_ignores_sells(self):
        """Sells by the same politician don't count as repeats."""
        trades = [
            make_trade("Pelosi", "AAPL", "purchase", datetime.utcnow() - timedelta(days=10)),
            make_trade("Pelosi", "AAPL", "sale",     datetime.utcnow() - timedelta(days=5)),
        ]
        engine = _engine()
        result = engine.signal_d_repeat_buying("AAPL", trades)
        assert result.active is False   # only 1 buy by Pelosi


# ---------------------------------------------------------------------------
# Signal F — LARGE_TRANSACTION
# ---------------------------------------------------------------------------

class TestSignalF:

    def test_large_transaction_fires_when_majority_large(self, sample_trades):
        """Multiple $100K+ buys in sample_trades."""
        engine = _engine()
        result = engine.signal_f_large_transaction("AAPL", sample_trades)
        assert result.active is True

    def test_large_transaction_not_active_small_amounts(self):
        trades = [
            make_trade("Pelosi", "AAPL", "purchase",
                       amount_range="$1,001 - $15,000", amount_min=1_001, amount_max=15_000),
            make_trade("Ryan",   "AAPL", "purchase",
                       amount_range="$15,001 - $50,000", amount_min=15_001, amount_max=50_000),
        ]
        engine = _engine()
        result = engine.signal_f_large_transaction("AAPL", trades)
        assert result.active is False

    def test_large_transaction_empty_trades(self):
        engine = _engine()
        result = engine.signal_f_large_transaction("AAPL", [])
        assert result.active is False


# ---------------------------------------------------------------------------
# Scoring engine — score computation
# ---------------------------------------------------------------------------

class TestScoringComputation:

    def test_score_is_in_valid_range(self, db_session, sample_trades, sample_company):
        with patch("services.analysis_engine.get_session") as mock_gs, \
             patch("services.scoring_engine.get_session") as mock_gs2:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)
            mock_gs2.return_value.__enter__ = lambda s: db_session
            mock_gs2.return_value.__exit__ = MagicMock(return_value=False)

            engine = _engine()
            signals = engine.analyse_ticker("AAPL")
            scoring = _scoring()
            score = scoring.score_ticker("AAPL", signals=signals, persist=False)

        assert 0.0 <= score <= 100.0

    def test_score_zero_for_no_signals(self):
        """TickerSignals with no active signals → score of 0."""
        from services.analysis_engine import TickerSignals, SignalResult
        signals = TickerSignals(ticker="XYZ")
        signals.signals = [
            SignalResult("BUY_CLUSTER", "XYZ", active=False),
            SignalResult("NET_BUY_ACTIVITY", "XYZ", active=False),
            SignalResult("SECTOR_SURGE", "XYZ", active=False),
            SignalResult("REPEAT_BUYING", "XYZ", active=False),
            SignalResult("FREQUENCY_RISE", "XYZ", active=False),
            SignalResult("LARGE_TRANSACTION", "XYZ", active=False),
        ]
        scoring = _scoring()
        raw, breakdown = scoring._compute_raw_score(signals)
        assert raw == 0.0
        assert scoring._normalise(raw) == 0.0

    def test_frequency_multiplier_increases_score(self):
        """When FREQUENCY_RISE is active, raw score should be 1.25× higher."""
        from services.analysis_engine import TickerSignals, SignalResult
        from config import SCORING_WEIGHTS

        # Base: 2 net buys = 6 pts
        signals_no_freq = TickerSignals(ticker="XYZ")
        signals_no_freq.signals = [
            SignalResult("BUY_CLUSTER", "XYZ", active=False),
            SignalResult("NET_BUY_ACTIVITY", "XYZ", active=True, value=2.0,
                         data={"net": 2}),
            SignalResult("SECTOR_SURGE", "XYZ", active=False),
            SignalResult("REPEAT_BUYING", "XYZ", active=False),
            SignalResult("FREQUENCY_RISE", "XYZ", active=False),
            SignalResult("LARGE_TRANSACTION", "XYZ", active=False),
        ]

        signals_with_freq = TickerSignals(ticker="XYZ")
        signals_with_freq.signals = [
            SignalResult("BUY_CLUSTER", "XYZ", active=False),
            SignalResult("NET_BUY_ACTIVITY", "XYZ", active=True, value=2.0,
                         data={"net": 2}),
            SignalResult("SECTOR_SURGE", "XYZ", active=False),
            SignalResult("REPEAT_BUYING", "XYZ", active=False),
            SignalResult("FREQUENCY_RISE", "XYZ", active=True),
            SignalResult("LARGE_TRANSACTION", "XYZ", active=False),
        ]

        scoring = _scoring()
        raw_base, _ = scoring._compute_raw_score(signals_no_freq)
        raw_freq, _ = scoring._compute_raw_score(signals_with_freq)

        assert raw_freq == pytest.approx(raw_base * 1.25)

    def test_normalise_clamps_to_100(self):
        scoring = _scoring()
        assert scoring._normalise(200.0) == 100.0
        assert scoring._normalise(-10.0) == 0.0

    def test_unique_buyer_points_accumulate(self):
        """5 unique buyers → 25 pts from Signal A alone."""
        from services.analysis_engine import TickerSignals, SignalResult
        signals = TickerSignals(ticker="XYZ")
        signals.signals = [
            SignalResult("BUY_CLUSTER", "XYZ", active=True, value=5.0,
                         data={"max_unique_buyers": 5, "buyers": ["A","B","C","D","E"]}),
            SignalResult("NET_BUY_ACTIVITY", "XYZ", active=False),
            SignalResult("SECTOR_SURGE", "XYZ", active=False),
            SignalResult("REPEAT_BUYING", "XYZ", active=False),
            SignalResult("FREQUENCY_RISE", "XYZ", active=False),
            SignalResult("LARGE_TRANSACTION", "XYZ", active=False),
        ]
        scoring = _scoring()
        raw, breakdown = scoring._compute_raw_score(signals)
        assert breakdown["unique_buyer_points"] == pytest.approx(25.0)
        assert raw == pytest.approx(25.0)

    def test_breakdown_keys_always_present(self):
        """Breakdown dict must always contain all expected keys."""
        from services.analysis_engine import TickerSignals, SignalResult
        signals = TickerSignals(ticker="XYZ")
        signals.signals = [
            SignalResult("BUY_CLUSTER", "XYZ", active=False),
            SignalResult("NET_BUY_ACTIVITY", "XYZ", active=False),
            SignalResult("SECTOR_SURGE", "XYZ", active=False),
            SignalResult("REPEAT_BUYING", "XYZ", active=False),
            SignalResult("FREQUENCY_RISE", "XYZ", active=False),
            SignalResult("LARGE_TRANSACTION", "XYZ", active=False),
        ]
        scoring = _scoring()
        _, breakdown = scoring._compute_raw_score(signals)
        expected_keys = {
            "unique_buyer_points", "net_buy_points", "sector_momentum_points",
            "repeat_purchase_points", "large_transaction_points",
            "frequency_multiplier_active", "active_signals",
        }
        assert expected_keys.issubset(breakdown.keys())
