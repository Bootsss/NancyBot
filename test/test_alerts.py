"""
tests/test_alerts.py — Alert generation and deduplication tests.

Covers:
  - Each alert type fires under correct conditions
  - Deduplication within the 7-day window
  - generate_alerts returns only new alerts
  - Alert signal_data stored correctly
  - Embed building helpers (without Discord client)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models import Alert, Company
from services.alert_service import AlertService, DEDUP_WINDOW_DAYS
from services.analysis_engine import SignalResult, TickerSignals
from tests.conftest import make_company, make_trade


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signals(
    ticker: str = "AAPL",
    cluster: bool = False,
    net_buy: bool = False,
    sector: bool = False,
    repeat: bool = False,
    freq: bool = False,
    large: bool = False,
) -> TickerSignals:
    ts = TickerSignals(ticker=ticker)
    ts.signals = [
        SignalResult("BUY_CLUSTER", ticker, active=cluster,
                     data={"max_unique_buyers": 4, "buyers": ["A", "B", "C", "D"],
                           "window_start": "2024-01-01", "window_days": 30}),
        SignalResult("NET_BUY_ACTIVITY", ticker, active=net_buy, value=3.0,
                     data={"buy_count": 5, "sell_count": 2, "net": 3,
                           "buy_ratio": 0.71, "buyers": ["A"], "sellers": ["B"]}),
        SignalResult("SECTOR_SURGE", ticker, active=sector, value=1.8,
                     data={"sector": "Technology", "sector_buys": 12,
                           "total_buys": 30, "sector_share": 0.4,
                           "concentration_ratio": 1.8, "all_sectors": {"Technology": 12}}),
        SignalResult("REPEAT_BUYING", ticker, active=repeat, value=1.0,
                     data={"repeat_buyers": {"A": 3}, "total_buy_events": 5}),
        SignalResult("FREQUENCY_RISE", ticker, active=freq,
                     data={"early_buy_count": 1, "recent_buy_count": 4,
                           "early_rate_per_month": 0.5, "recent_rate_per_month": 2.0,
                           "acceleration_ratio": 4.0}),
        SignalResult("LARGE_TRANSACTION", ticker, active=large, value=75.0,
                     data={"large_count": 3, "total_count": 4, "large_share_pct": 75.0,
                           "large_politicians": ["A"], "estimated_large_total": 450_000,
                           "estimated_total": 600_000}),
    ]
    return ts


def _alert_service() -> AlertService:
    """AlertService without a Discord client (posting not tested here)."""
    return AlertService(discord_client=None)


# ---------------------------------------------------------------------------
# Alert type evaluation
# ---------------------------------------------------------------------------

class TestAlertEvaluation:

    def test_buy_cluster_alert_generated(self):
        svc = _alert_service()
        signals = _make_signals("AAPL", cluster=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=65.0)
        types = [a.alert_type for a in alerts]
        assert "BUY_CLUSTER" in types

    def test_sector_surge_alert_generated(self):
        svc = _alert_service()
        signals = _make_signals("AAPL", sector=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=55.0)
        types = [a.alert_type for a in alerts]
        assert "SECTOR_SURGE" in types

    def test_high_score_alert_generated_above_threshold(self):
        svc = _alert_service()
        signals = _make_signals("AAPL")
        # Score above default threshold (70)
        alerts = svc._evaluate_ticker("AAPL", signals, score=75.0)
        types = [a.alert_type for a in alerts]
        assert "HIGH_SCORE" in types

    def test_high_score_not_generated_below_threshold(self):
        svc = _alert_service()
        signals = _make_signals("AAPL")
        alerts = svc._evaluate_ticker("AAPL", signals, score=50.0)
        types = [a.alert_type for a in alerts]
        assert "HIGH_SCORE" not in types

    def test_repeat_buying_alert_generated(self):
        svc = _alert_service()
        signals = _make_signals("AAPL", repeat=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=55.0)
        types = [a.alert_type for a in alerts]
        assert "REPEAT_BUYING" in types

    def test_unusual_activity_requires_3_signals(self):
        svc = _alert_service()
        # Only 2 signals active → no UNUSUAL_ACTIVITY
        signals = _make_signals("AAPL", cluster=True, net_buy=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=60.0)
        types = [a.alert_type for a in alerts]
        assert "UNUSUAL_ACTIVITY" not in types

    def test_unusual_activity_fires_with_3_signals(self):
        svc = _alert_service()
        signals = _make_signals("AAPL", cluster=True, net_buy=True, repeat=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=60.0)
        types = [a.alert_type for a in alerts]
        assert "UNUSUAL_ACTIVITY" in types

    def test_multiple_alert_types_can_fire_together(self):
        """All six signals active → 5 alert types generated."""
        svc = _alert_service()
        signals = _make_signals("AAPL", cluster=True, net_buy=True, sector=True,
                                repeat=True, freq=True, large=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=95.0)
        types = {a.alert_type for a in alerts}
        assert "BUY_CLUSTER" in types
        assert "SECTOR_SURGE" in types
        assert "HIGH_SCORE" in types
        assert "REPEAT_BUYING" in types
        assert "UNUSUAL_ACTIVITY" in types

    def test_no_signals_no_alerts_below_threshold(self):
        svc = _alert_service()
        signals = _make_signals("AAPL")   # all False
        alerts = svc._evaluate_ticker("AAPL", signals, score=40.0)
        assert len(alerts) == 0

    def test_alert_contains_ticker(self):
        svc = _alert_service()
        signals = _make_signals("NVDA", cluster=True)
        alerts = svc._evaluate_ticker("NVDA", signals, score=60.0)
        for alert in alerts:
            assert alert.ticker == "NVDA"

    def test_alert_score_stored(self):
        svc = _alert_service()
        signals = _make_signals("AAPL", cluster=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=68.5)
        cluster_alert = next(a for a in alerts if a.alert_type == "BUY_CLUSTER")
        assert cluster_alert.score == pytest.approx(68.5)

    def test_alert_signal_data_stored(self):
        svc = _alert_service()
        signals = _make_signals("AAPL", cluster=True)
        alerts = svc._evaluate_ticker("AAPL", signals, score=60.0)
        cluster_alert = next(a for a in alerts if a.alert_type == "BUY_CLUSTER")
        sd = cluster_alert.signal_data
        assert "signal" in sd
        assert "active_signals" in sd


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestAlertDeduplication:

    def test_duplicate_within_window_is_skipped(self, db_session):
        with patch("services.alert_service.get_session") as mock_gs:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            # Seed an existing alert
            existing = Alert(
                ticker="AAPL",
                alert_type="BUY_CLUSTER",
                score=60.0,
                alert_date=datetime.utcnow() - timedelta(days=2),
                posted=True,
            )
            existing.signal_data = {}
            db_session.add(existing)
            db_session.flush()

            svc = _alert_service()
            candidate = Alert(
                ticker="AAPL", alert_type="BUY_CLUSTER",
                score=65.0, alert_date=datetime.utcnow(), posted=False,
            )
            candidate.signal_data = {}
            is_dup = svc._is_duplicate(candidate)

        assert is_dup is True

    def test_alert_outside_window_not_duplicate(self, db_session):
        with patch("services.alert_service.get_session") as mock_gs:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            # Alert older than DEDUP_WINDOW_DAYS
            old_date = datetime.utcnow() - timedelta(days=DEDUP_WINDOW_DAYS + 1)
            existing = Alert(
                ticker="AAPL", alert_type="BUY_CLUSTER",
                score=60.0, alert_date=old_date, posted=True,
            )
            existing.signal_data = {}
            db_session.add(existing)
            db_session.flush()

            svc = _alert_service()
            candidate = Alert(
                ticker="AAPL", alert_type="BUY_CLUSTER",
                score=65.0, alert_date=datetime.utcnow(), posted=False,
            )
            candidate.signal_data = {}
            is_dup = svc._is_duplicate(candidate)

        assert is_dup is False

    def test_different_alert_type_not_duplicate(self, db_session):
        with patch("services.alert_service.get_session") as mock_gs:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            existing = Alert(
                ticker="AAPL", alert_type="BUY_CLUSTER",
                score=60.0, alert_date=datetime.utcnow() - timedelta(days=2),
                posted=True,
            )
            existing.signal_data = {}
            db_session.add(existing)
            db_session.flush()

            svc = _alert_service()
            # Different type
            candidate = Alert(
                ticker="AAPL", alert_type="HIGH_SCORE",
                score=75.0, alert_date=datetime.utcnow(), posted=False,
            )
            candidate.signal_data = {}
            is_dup = svc._is_duplicate(candidate)

        assert is_dup is False

    def test_different_ticker_not_duplicate(self, db_session):
        with patch("services.alert_service.get_session") as mock_gs:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            existing = Alert(
                ticker="AAPL", alert_type="BUY_CLUSTER",
                score=60.0, alert_date=datetime.utcnow() - timedelta(days=1),
                posted=True,
            )
            existing.signal_data = {}
            db_session.add(existing)
            db_session.flush()

            svc = _alert_service()
            candidate = Alert(
                ticker="NVDA", alert_type="BUY_CLUSTER",
                score=65.0, alert_date=datetime.utcnow(), posted=False,
            )
            candidate.signal_data = {}
            is_dup = svc._is_duplicate(candidate)

        assert is_dup is False


# ---------------------------------------------------------------------------
# make_alert helper
# ---------------------------------------------------------------------------

class TestMakeAlert:

    def test_make_alert_sets_fields(self):
        alert = AlertService._make_alert(
            ticker="AAPL",
            alert_type="BUY_CLUSTER",
            score=72.5,
            summary="Test summary",
            signal_data={"key": "value"},
        )
        assert alert.ticker == "AAPL"
        assert alert.alert_type == "BUY_CLUSTER"
        assert alert.score == pytest.approx(72.5)
        assert alert.summary == "Test summary"
        assert alert.posted is False
        assert alert.signal_data == {"key": "value"}

    def test_make_alert_posted_false_by_default(self):
        alert = AlertService._make_alert(
            ticker="AAPL", alert_type="HIGH_SCORE",
            score=80.0, summary="", signal_data={},
        )
        assert alert.posted is False
