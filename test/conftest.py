"""
tests/conftest.py — Shared pytest fixtures.

Provides:
    db_session   — SQLite in-memory session, rolled back after each test
    sample_trades — list of Trade objects covering buy/sell/edge cases
    sample_company — a Company row for AAPL
    populated_db  — db_session pre-loaded with sample data
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# Point at an in-memory SQLite DB before any app module imports
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DISCORD_TOKEN", "test_token")
os.environ.setdefault("DISCORD_CHANNEL_ID", "123456789")

from models import Base, Alert, Backtest, Company, Score, Trade


# ---------------------------------------------------------------------------
# In-memory database
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def db_engine():
    """Create a fresh in-memory SQLite engine per test function."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine) -> Session:
    """
    Yield a transactional session that rolls back after each test.

    Patches database.get_session() so all service code uses this
    in-memory session rather than the real database.
    """
    TestSession = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = TestSession()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# Sample data factories
# ---------------------------------------------------------------------------

def make_trade(
    politician_name: str = "Nancy Pelosi",
    ticker: str = "AAPL",
    trade_type: str = "purchase",
    trade_date: datetime | None = None,
    company_name: str = "Apple Inc.",
    amount_range: str = "$50,001 - $100,000",
    amount_min: float = 50_001,
    amount_max: float = 100_000,
    politician_party: str = "Democrat",
    politician_chamber: str = "House",
) -> Trade:
    return Trade(
        politician_name=politician_name,
        politician_party=politician_party,
        politician_chamber=politician_chamber,
        company_name=company_name,
        ticker=ticker,
        trade_type=trade_type,
        trade_date=trade_date or datetime.utcnow() - timedelta(days=5),
        disclosure_date=datetime.utcnow() - timedelta(days=2),
        amount_range=amount_range,
        amount_min=amount_min,
        amount_max=amount_max,
        source_url="https://test.example.com",
    )


def make_company(
    ticker: str = "AAPL",
    company_name: str = "Apple Inc.",
    sector: str = "Technology",
    industry: str = "Consumer Electronics",
    market_cap: float = 3_000_000_000_000,
    current_price: float = 195.50,
    pe_ratio: float = 28.5,
    week_52_return: float = 32.4,
    avg_volume: float = 55_000_000,
) -> Company:
    return Company(
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        industry=industry,
        market_cap=market_cap,
        current_price=current_price,
        pe_ratio=pe_ratio,
        week_52_return=week_52_return,
        avg_volume=avg_volume,
        ticker_verified=True,
        manually_overridden=False,
        last_updated=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_company(db_session: Session) -> Company:
    """AAPL Company row, persisted to the in-memory DB."""
    company = make_company()
    db_session.add(company)
    db_session.flush()
    return company


@pytest.fixture
def sample_trades(db_session: Session, sample_company: Company) -> list[Trade]:
    """
    A realistic set of Trade rows covering:
      - Multiple politicians buying AAPL (buy cluster)
      - One sell trade
      - A repeat buyer
      - Large and small amount ranges
      - Trades spread across 45 days
    """
    now = datetime.utcnow()
    trades = [
        # Cluster of 4 buyers within 25 days
        make_trade("Nancy Pelosi",   "AAPL", "purchase", now - timedelta(days=25),
                   amount_range="$500,001 - $1,000,000", amount_min=500_001, amount_max=1_000_000),
        make_trade("Paul Ryan",      "AAPL", "purchase", now - timedelta(days=20),
                   amount_range="$100,001 - $250,000", amount_min=100_001, amount_max=250_000),
        make_trade("Mitch McConnell","AAPL", "purchase", now - timedelta(days=15),
                   amount_range="$50,001 - $100,000", amount_min=50_001, amount_max=100_000),
        make_trade("Kevin McCarthy", "AAPL", "purchase", now - timedelta(days=10),
                   amount_range="$100,001 - $250,000", amount_min=100_001, amount_max=250_000),
        # Repeat buyer
        make_trade("Nancy Pelosi",   "AAPL", "purchase", now - timedelta(days=5),
                   amount_range="$250,001 - $500,000", amount_min=250_001, amount_max=500_000),
        # One sell
        make_trade("Chuck Schumer",  "AAPL", "sale",     now - timedelta(days=8),
                   amount_range="$15,001 - $50,000", amount_min=15_001, amount_max=50_000),
    ]
    for t in trades:
        db_session.add(t)
    db_session.flush()
    return trades


@pytest.fixture
def nvda_trades(db_session: Session) -> list[Trade]:
    """Small set of NVDA trades (not a cluster) for comparison tests."""
    nvda = make_company(
        ticker="NVDA", company_name="NVIDIA Corporation",
        sector="Technology", industry="Semiconductors",
    )
    db_session.add(nvda)

    now = datetime.utcnow()
    trades = [
        make_trade("Nancy Pelosi", "NVDA", "purchase", now - timedelta(days=40),
                   company_name="NVIDIA Corporation"),
        make_trade("Nancy Pelosi", "NVDA", "sale",     now - timedelta(days=10),
                   company_name="NVIDIA Corporation"),
    ]
    for t in trades:
        db_session.add(t)
    db_session.flush()
    return trades


@pytest.fixture
def populated_db(db_session: Session, sample_trades: list[Trade], nvda_trades: list[Trade]) -> Session:
    """Session pre-loaded with companies + trades for both AAPL and NVDA."""
    return db_session


@pytest.fixture
def sample_score(db_session: Session, sample_company: Company) -> Score:
    """A Score row for AAPL."""
    score = Score(ticker="AAPL", score=72.5, calculated_at=datetime.utcnow())
    score.breakdown = {
        "unique_buyer_points": 20.0,
        "net_buy_points": 12.0,
        "sector_momentum_points": 10.0,
        "repeat_purchase_points": 5.0,
        "large_transaction_points": 5.0,
        "frequency_multiplier_active": 1.0,
        "active_signals": ["BUY_CLUSTER", "NET_BUY_ACTIVITY", "REPEAT_BUYING",
                           "LARGE_TRANSACTION", "FREQUENCY_RISE"],
        "raw_score": 62.4,
        "normalised_score": 72.5,
    }
    db_session.add(score)
    db_session.flush()
    return score


@pytest.fixture
def sample_alert(db_session: Session, sample_company: Company) -> Alert:
    """A BUY_CLUSTER alert for AAPL."""
    alert = Alert(
        ticker="AAPL",
        alert_type="BUY_CLUSTER",
        score=72.5,
        summary="4 politicians bought $AAPL within 30 days.",
        alert_date=datetime.utcnow() - timedelta(days=1),
        posted=False,
    )
    alert.signal_data = {
        "signal": {"max_unique_buyers": 4, "buyers": ["Nancy Pelosi", "Paul Ryan"]},
        "active_signals": ["BUY_CLUSTER", "NET_BUY_ACTIVITY"],
    }
    db_session.add(alert)
    db_session.flush()
    return alert
