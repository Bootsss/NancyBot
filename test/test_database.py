"""
tests/test_database.py — Database model and session tests.

Covers:
  - Trade creation, unique constraint enforcement
  - Company upsert behaviour
  - Score serialisation / deserialisation
  - Alert signal_data round-trip
  - Backtest relationships
  - Amount range parsing helpers
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models import Alert, Backtest, Company, Score, Trade
from tests.conftest import make_company, make_trade


# ---------------------------------------------------------------------------
# Trade model
# ---------------------------------------------------------------------------

class TestTradeModel:

    def test_create_trade(self, db_session):
        trade = make_trade()
        db_session.add(trade)
        db_session.flush()
        assert trade.id is not None
        assert trade.ticker == "AAPL"
        assert trade.politician_name == "Nancy Pelosi"

    def test_is_buy_property(self, db_session):
        buy = make_trade(trade_type="purchase")
        sell = make_trade(trade_type="sale")
        db_session.add_all([buy, sell])
        db_session.flush()
        assert buy.is_buy is True
        assert buy.is_sell is False
        assert sell.is_buy is False
        assert sell.is_sell is True

    def test_amount_midpoint(self, db_session):
        trade = make_trade(amount_min=50_001, amount_max=100_000)
        assert trade.amount_midpoint == pytest.approx(75_000.5)

    def test_amount_midpoint_none_when_missing(self, db_session):
        trade = make_trade()
        trade.amount_min = None
        trade.amount_max = None
        assert trade.amount_midpoint is None

    def test_amount_midpoint_single_bound(self, db_session):
        trade = make_trade()
        trade.amount_min = 50_000
        trade.amount_max = None
        assert trade.amount_midpoint == 50_000

    def test_unique_constraint_prevents_duplicate(self, db_session):
        """Inserting the same (politician, ticker, date, type) twice raises IntegrityError."""
        trade_date = datetime(2024, 1, 15)
        t1 = make_trade(trade_date=trade_date)
        t2 = make_trade(trade_date=trade_date)   # identical identity fields
        db_session.add(t1)
        db_session.flush()

        db_session.add(t2)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_unique_constraint_allows_different_type(self, db_session):
        """Same politician/ticker/date but different trade_type → allowed."""
        trade_date = datetime(2024, 1, 15)
        buy = make_trade(trade_type="purchase", trade_date=trade_date)
        sell = make_trade(trade_type="sale", trade_date=trade_date)
        db_session.add_all([buy, sell])
        db_session.flush()   # should not raise
        assert buy.id is not None
        assert sell.id is not None

    def test_unique_constraint_allows_different_politician(self, db_session):
        """Different politician → not a duplicate."""
        trade_date = datetime(2024, 1, 15)
        t1 = make_trade(politician_name="Nancy Pelosi", trade_date=trade_date)
        t2 = make_trade(politician_name="Mitch McConnell", trade_date=trade_date)
        db_session.add_all([t1, t2])
        db_session.flush()
        assert t1.id is not None
        assert t2.id is not None

    def test_trade_repr(self, db_session):
        trade = make_trade()
        db_session.add(trade)
        db_session.flush()
        r = repr(trade)
        assert "AAPL" in r
        assert "Nancy Pelosi" in r

    def test_nullable_ticker(self, db_session):
        """Trades without a resolved ticker are valid."""
        trade = make_trade()
        trade.ticker = None
        db_session.add(trade)
        db_session.flush()
        assert trade.id is not None
        assert trade.ticker is None


# ---------------------------------------------------------------------------
# Company model
# ---------------------------------------------------------------------------

class TestCompanyModel:

    def test_create_company(self, db_session):
        company = make_company()
        db_session.add(company)
        db_session.flush()
        fetched = db_session.get(Company, "AAPL")
        assert fetched is not None
        assert fetched.company_name == "Apple Inc."
        assert fetched.sector == "Technology"

    def test_company_primary_key_is_ticker(self, db_session):
        company = make_company(ticker="MSFT", company_name="Microsoft Corporation")
        db_session.add(company)
        db_session.flush()
        assert db_session.get(Company, "MSFT") is not None
        assert db_session.get(Company, "AAPL") is None

    def test_company_repr(self, db_session):
        company = make_company()
        r = repr(company)
        assert "AAPL" in r
        assert "Technology" in r

    def test_manually_overridden_flag(self, db_session):
        company = make_company()
        company.manually_overridden = True
        db_session.add(company)
        db_session.flush()
        fetched = db_session.get(Company, "AAPL")
        assert fetched.manually_overridden is True


# ---------------------------------------------------------------------------
# Score model
# ---------------------------------------------------------------------------

class TestScoreModel:

    def test_create_score(self, db_session, sample_company):
        score = Score(ticker="AAPL", score=65.0, calculated_at=datetime.utcnow())
        score.breakdown = {"unique_buyer_points": 15.0, "net_buy_points": 9.0}
        db_session.add(score)
        db_session.flush()
        assert score.id is not None

    def test_breakdown_round_trip(self, db_session, sample_company):
        """breakdown setter serialises to JSON; getter deserialises back."""
        original = {
            "unique_buyer_points": 20.0,
            "net_buy_points": 12.0,
            "active_signals": ["BUY_CLUSTER", "NET_BUY_ACTIVITY"],
        }
        score = Score(ticker="AAPL", score=72.5, calculated_at=datetime.utcnow())
        score.breakdown = original
        db_session.add(score)
        db_session.flush()

        fetched = db_session.get(Score, score.id)
        assert fetched.breakdown == original

    def test_breakdown_empty_dict_when_null(self, db_session, sample_company):
        score = Score(ticker="AAPL", score=0.0, calculated_at=datetime.utcnow())
        db_session.add(score)
        db_session.flush()
        assert score.breakdown == {}

    def test_score_repr(self, db_session, sample_company):
        score = Score(ticker="AAPL", score=55.0, calculated_at=datetime.utcnow())
        db_session.add(score)
        db_session.flush()
        assert "AAPL" in repr(score)
        assert "55.0" in repr(score)

    def test_multiple_scores_for_same_ticker(self, db_session, sample_company):
        """Score history: multiple rows per ticker allowed."""
        for score_val in [30.0, 50.0, 70.0]:
            s = Score(ticker="AAPL", score=score_val, calculated_at=datetime.utcnow())
            db_session.add(s)
        db_session.flush()

        stmt = select(Score).where(Score.ticker == "AAPL")
        rows = db_session.scalars(stmt).all()
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

class TestAlertModel:

    def test_create_alert(self, db_session, sample_company):
        alert = Alert(
            ticker="AAPL",
            alert_type="BUY_CLUSTER",
            score=72.5,
            summary="4 politicians bought AAPL.",
            alert_date=datetime.utcnow(),
            posted=False,
        )
        alert.signal_data = {"buyers": ["Pelosi", "Ryan"]}
        db_session.add(alert)
        db_session.flush()
        assert alert.id is not None

    def test_signal_data_round_trip(self, db_session, sample_company):
        original = {
            "signal": {"max_unique_buyers": 4},
            "active_signals": ["BUY_CLUSTER"],
        }
        alert = Alert(
            ticker="AAPL", alert_type="BUY_CLUSTER",
            score=65.0, alert_date=datetime.utcnow(), posted=False,
        )
        alert.signal_data = original
        db_session.add(alert)
        db_session.flush()

        fetched = db_session.get(Alert, alert.id)
        assert fetched.signal_data == original

    def test_alert_posted_defaults_false(self, db_session, sample_company):
        alert = Alert(
            ticker="AAPL", alert_type="HIGH_SCORE",
            score=80.0, alert_date=datetime.utcnow(),
        )
        db_session.add(alert)
        db_session.flush()
        assert alert.posted is False

    def test_alert_repr(self, db_session, sample_company):
        alert = Alert(
            ticker="AAPL", alert_type="HIGH_SCORE",
            score=80.0, alert_date=datetime.utcnow(), posted=False,
        )
        db_session.add(alert)
        db_session.flush()
        r = repr(alert)
        assert "AAPL" in r
        assert "HIGH_SCORE" in r


# ---------------------------------------------------------------------------
# Backtest model
# ---------------------------------------------------------------------------

class TestBacktestModel:

    def test_create_backtest(self, db_session, sample_company):
        alert = Alert(
            ticker="AAPL", alert_type="BUY_CLUSTER",
            score=72.5, alert_date=datetime.utcnow() - timedelta(days=35),
            posted=True,
        )
        db_session.add(alert)
        db_session.flush()

        bt = Backtest(
            alert_id=alert.id,
            ticker="AAPL",
            entry_price=180.0,
            return_30d=5.2,
            return_90d=None,
            return_180d=None,
            sp500_return_30d=2.1,
            sp500_return_90d=None,
            sp500_return_180d=None,
            beat_market_30d=True,
            beat_market_90d=None,
            beat_market_180d=None,
            calculated_at=datetime.utcnow(),
        )
        db_session.add(bt)
        db_session.flush()
        assert bt.id is not None
        assert bt.beat_market_30d is True

    def test_backtest_repr(self, db_session, sample_company):
        alert = Alert(
            ticker="AAPL", alert_type="BUY_CLUSTER",
            score=72.5, alert_date=datetime.utcnow() - timedelta(days=35),
            posted=True,
        )
        db_session.add(alert)
        db_session.flush()

        bt = Backtest(
            alert_id=alert.id, ticker="AAPL",
            entry_price=180.0, return_30d=5.2,
            calculated_at=datetime.utcnow(),
        )
        db_session.add(bt)
        db_session.flush()
        r = repr(bt)
        assert "AAPL" in r
        assert "5.2" in r
