"""
models.py — SQLAlchemy ORM models for Capitol Gains.

Tables:
    Trade     — individual congressional stock disclosures
    Company   — enriched company/ticker metadata
    Alert     — generated trading alerts
    Score     — scored ticker snapshots with breakdown
    Backtest  — historical alert performance results
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Shared declarative base for all models."""
    pass


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class Trade(Base):
    """
    One row per congressional stock trade disclosure.

    A single disclosure event on Capitol Hill can cover multiple tickers;
    each ticker gets its own row here.  The (politician_name, ticker,
    trade_date, trade_type) combination is enforced as unique to prevent
    duplicate imports from re-runs.
    """

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint(
            "politician_name",
            "ticker",
            "trade_date",
            "trade_type",
            name="uq_trade_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Who made the trade
    politician_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    politician_party: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    politician_chamber: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # House | Senate

    # What they traded
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)
    ticker: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)

    # Trade details
    trade_type: Mapped[str] = mapped_column(String(20), nullable=False)   # purchase | sale | exchange
    trade_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    disclosure_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Amount reported as a range per STOCK Act rules
    amount_range: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    amount_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Provenance
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    raw_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON blob of original payload

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Relationship
    company: Mapped[Optional["Company"]] = relationship(
        "Company", foreign_keys=[ticker], primaryjoin="Trade.ticker == Company.ticker", viewonly=True
    )

    def __repr__(self) -> str:
        return (
            f"<Trade id={self.id} politician={self.politician_name!r} "
            f"ticker={self.ticker!r} type={self.trade_type!r} date={self.trade_date.date()}>"
        )

    @property
    def is_buy(self) -> bool:
        return self.trade_type.lower() == "purchase"

    @property
    def is_sell(self) -> bool:
        return self.trade_type.lower() == "sale"

    @property
    def amount_midpoint(self) -> Optional[float]:
        """Return midpoint of reported amount range, or None."""
        if self.amount_min is not None and self.amount_max is not None:
            return (self.amount_min + self.amount_max) / 2
        return self.amount_min or self.amount_max


# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------

class Company(Base):
    """
    Enriched metadata for a ticker symbol.

    Populated by the ticker_mapper service (sector/industry) and the
    market_data service (financials).  Updated weekly.
    """

    __tablename__ = "companies"

    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)

    # Classification
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    industry: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    # Market data snapshot
    market_cap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # YoY %
    week_52_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    week_52_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    week_52_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # % return
    avg_volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Flags
    ticker_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manually_overridden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Relationships
    scores: Mapped[list["Score"]] = relationship("Score", back_populates="company", order_by="Score.calculated_at.desc()")
    alerts: Mapped[list["Alert"]] = relationship("Alert", back_populates="company", order_by="Alert.alert_date.desc()")

    def __repr__(self) -> str:
        return f"<Company ticker={self.ticker!r} name={self.company_name!r} sector={self.sector!r}>"


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

class Score(Base):
    """
    Point-in-time composite score for a ticker (0–100).

    A new row is inserted each time the scoring engine runs so that
    score history is preserved for trend analysis and backtesting.
    """

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.ticker", ondelete="CASCADE"), nullable=False, index=True
    )

    score: Mapped[float] = mapped_column(Float, nullable=False)

    # Raw component values stored as JSON for transparency and debugging
    # e.g. {"unique_buyers": 3, "net_buy_activity": 5, "repeat_purchases": 1, ...}
    breakdown_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    calculated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )

    # Relationship
    company: Mapped["Company"] = relationship("Company", back_populates="scores")

    def __repr__(self) -> str:
        return f"<Score ticker={self.ticker!r} score={self.score:.1f} at={self.calculated_at}>"

    @property
    def breakdown(self) -> dict:
        """Deserialise breakdown_json to a Python dict."""
        if self.breakdown_json:
            return json.loads(self.breakdown_json)
        return {}

    @breakdown.setter
    def breakdown(self, value: dict) -> None:
        self.breakdown_json = json.dumps(value)


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

class Alert(Base):
    """
    A trading alert generated by the analysis engine.

    Alert types:
        BUY_CLUSTER      — 3+ politicians buy same stock within 30 days
        SECTOR_SURGE     — unusual concentration in one sector
        HIGH_SCORE       — ticker crosses score threshold
        REPEAT_BUYING    — same politician buys same stock repeatedly
        UNUSUAL_ACTIVITY — statistical outlier in trade volume/frequency
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(
        String(20), ForeignKey("companies.ticker", ondelete="CASCADE"), nullable=False, index=True
    )

    alert_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)

    # Human-readable summary posted to Discord
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Structured signal data (JSON) used to build the Discord embed
    signal_data_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    alert_date: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )

    # Whether this alert has been posted to Discord yet
    posted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationship
    company: Mapped["Company"] = relationship("Company", back_populates="alerts")
    backtest: Mapped[Optional["Backtest"]] = relationship("Backtest", back_populates="alert", uselist=False)

    def __repr__(self) -> str:
        return (
            f"<Alert id={self.id} ticker={self.ticker!r} "
            f"type={self.alert_type!r} score={self.score:.1f}>"
        )

    @property
    def signal_data(self) -> dict:
        if self.signal_data_json:
            return json.loads(self.signal_data_json)
        return {}

    @signal_data.setter
    def signal_data(self, value: dict) -> None:
        self.signal_data_json = json.dumps(value)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class Backtest(Base):
    """
    Post-hoc performance measurement for a generated alert.

    Populated by the monthly backtest job once enough price history
    exists (30 / 90 / 180 days after alert_date).
    """

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Price at alert date
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Returns vs baseline
    return_30d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # %
    return_90d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_180d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # S&P 500 (SPY) comparison over same windows
    sp500_return_30d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp500_return_90d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sp500_return_180d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Derived flags (set by backtest job)
    beat_market_30d: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    beat_market_90d: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    beat_market_180d: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    calculated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Relationship
    alert: Mapped["Alert"] = relationship("Alert", back_populates="backtest")

    def __repr__(self) -> str:
        return (
            f"<Backtest alert_id={self.alert_id} ticker={self.ticker!r} "
            f"30d={self.return_30d} 90d={self.return_90d} 180d={self.return_180d}>"
        )
