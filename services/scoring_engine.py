"""
services/scoring_engine.py — Composite scoring engine.

Converts TickerSignals from the analysis engine into a normalised 0–100
score, persists score history, and maintains five ranked watchlists.

Scoring formula
---------------
Each active signal contributes points based on weights in config.py:

    unique_buyers      × POINTS_PER_UNIQUE_BUYER       (Signal A)
    net_buy_count      × POINTS_PER_NET_BUY             (Signal B)
    repeat_purchase    + REPEAT_PURCHASE_BONUS          (Signal D)
    sector_momentum    + SECTOR_MOMENTUM_BONUS          (Signal C)
    large_transaction  + LARGE_TRANSACTION_BONUS        (Signal F)

Signal E (FREQUENCY_RISE) adds a multiplier rather than raw points:
    if active → multiply raw score by 1.25 before normalisation

Raw score is capped at SCORING_WEIGHTS.max_raw_score before being
normalised to 0–100.

Watchlists maintained
---------------------
    TOP_CONGRESSIONAL   Highest current scores
    TOP_SECTOR          Most active sectors by aggregate score
    MOST_BOUGHT         Tickers with highest buy count (last 30 days)
    FASTEST_RISING      Largest score increase vs previous snapshot
    MOST_IMPROVED       Largest absolute score gain over last 7 days
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import NamedTuple

from loguru import logger
from sqlalchemy import desc, func, select

from config import SCORING_WEIGHTS, settings
from database import get_session
from models import Company, Score, Trade
from services.analysis_engine import AnalysisEngine, TickerSignals, get_analysis_engine


# ---------------------------------------------------------------------------
# Watchlist entry
# ---------------------------------------------------------------------------

class WatchlistEntry(NamedTuple):
    ticker: str
    company_name: str
    sector: str | None
    score: float
    rank: int
    metadata: dict          # list-specific extra data


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

class ScoringEngine:
    """
    Converts analysis signals into scores and manages watchlists.

    Usage::

        engine = ScoringEngine()
        score = engine.score_ticker("AAPL")          # returns float 0–100
        engine.score_all()                           # score every active ticker
        watchlist = engine.get_watchlist("TOP_CONGRESSIONAL", limit=10)
    """

    WATCHLIST_NAMES = {
        "TOP_CONGRESSIONAL",
        "TOP_SECTOR",
        "MOST_BOUGHT",
        "FASTEST_RISING",
        "MOST_IMPROVED",
    }

    def __init__(self, analysis_engine: AnalysisEngine | None = None) -> None:
        self._analysis = analysis_engine or get_analysis_engine()

    # ------------------------------------------------------------------ #
    # Public scoring API
    # ------------------------------------------------------------------ #

    def score_ticker(
        self,
        ticker: str,
        signals: TickerSignals | None = None,
        persist: bool = True,
    ) -> float:
        """
        Compute and optionally persist a score for a single ticker.

        Parameters
        ----------
        ticker:  ticker symbol
        signals: pre-computed TickerSignals (avoids re-running analysis)
        persist: if True, insert a Score row into the database

        Returns
        -------
        Normalised score in the range [0.0, 100.0]
        """
        if signals is None:
            signals = self._analysis.analyse_ticker(ticker)

        raw_score, breakdown = self._compute_raw_score(signals)
        normalised = self._normalise(raw_score)

        breakdown["raw_score"] = round(raw_score, 4)
        breakdown["normalised_score"] = round(normalised, 4)

        logger.debug(
            "[ScoringEngine] {} → raw={:.2f} normalised={:.1f} breakdown={}",
            ticker,
            raw_score,
            normalised,
            breakdown,
        )

        if persist:
            self._persist_score(ticker, normalised, breakdown)

        return normalised

    def score_all(self, lookback_days: int = 90) -> dict[str, float]:
        """
        Score every ticker that has trades in the lookback window.

        Returns a dict of ticker → score, sorted descending by score.
        """
        logger.info("[ScoringEngine] Starting full scoring run.")
        all_signals = self._analysis.analyse_all(lookback_days=lookback_days)

        scores: dict[str, float] = {}
        for ticker_signals in all_signals:
            try:
                score = self.score_ticker(
                    ticker_signals.ticker,
                    signals=ticker_signals,
                    persist=True,
                )
                scores[ticker_signals.ticker] = score
            except Exception as exc:
                logger.error(
                    "[ScoringEngine] Failed to score {}: {}", ticker_signals.ticker, exc
                )

        sorted_scores = dict(
            sorted(scores.items(), key=lambda x: x[1], reverse=True)
        )
        logger.info(
            "[ScoringEngine] Scored {} tickers. Top 5: {}",
            len(sorted_scores),
            list(sorted_scores.items())[:5],
        )
        return sorted_scores

    def get_latest_score(self, ticker: str) -> Score | None:
        """Return the most recent Score row for a ticker, or None."""
        with get_session() as session:
            stmt = (
                select(Score)
                .where(Score.ticker == ticker)
                .order_by(desc(Score.calculated_at))
                .limit(1)
            )
            return session.scalars(stmt).first()

    def get_score_history(self, ticker: str, days: int = 30) -> list[Score]:
        """Return Score rows for a ticker over the last N days."""
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_session() as session:
            stmt = (
                select(Score)
                .where(Score.ticker == ticker, Score.calculated_at >= cutoff)
                .order_by(Score.calculated_at.asc())
            )
            return list(session.scalars(stmt).all())

    # ------------------------------------------------------------------ #
    # Watchlists
    # ------------------------------------------------------------------ #

    def get_watchlist(
        self, watchlist_name: str, limit: int = 10
    ) -> list[WatchlistEntry]:
        """
        Return the top N entries for the named watchlist.

        Supported names: TOP_CONGRESSIONAL, TOP_SECTOR, MOST_BOUGHT,
                         FASTEST_RISING, MOST_IMPROVED
        """
        if watchlist_name not in self.WATCHLIST_NAMES:
            raise ValueError(
                f"Unknown watchlist: {watchlist_name!r}. "
                f"Choose from: {self.WATCHLIST_NAMES}"
            )

        dispatch = {
            "TOP_CONGRESSIONAL": self._watchlist_top_congressional,
            "TOP_SECTOR":        self._watchlist_top_sector,
            "MOST_BOUGHT":       self._watchlist_most_bought,
            "FASTEST_RISING":    self._watchlist_fastest_rising,
            "MOST_IMPROVED":     self._watchlist_most_improved,
        }
        return dispatch[watchlist_name](limit)

    # ------------------------------------------------------------------ #
    # Score computation
    # ------------------------------------------------------------------ #

    def _compute_raw_score(self, signals: TickerSignals) -> tuple[float, dict]:
        """
        Apply the scoring formula to a TickerSignals object.

        Returns (raw_score, breakdown_dict).
        """
        w = SCORING_WEIGHTS
        breakdown: dict[str, float] = {}
        raw = 0.0

        # --- Signal A: unique buyers × 5 pts each ---
        cluster = signals.get("BUY_CLUSTER")
        if cluster and cluster.active:
            pts = cluster.data.get("max_unique_buyers", 0) * w.points_per_unique_buyer
            raw += pts
            breakdown["unique_buyer_points"] = round(pts, 2)
        else:
            breakdown["unique_buyer_points"] = 0.0

        # --- Signal B: net buys × 3 pts each ---
        net_buy = signals.get("NET_BUY_ACTIVITY")
        if net_buy and net_buy.active:
            net = max(net_buy.data.get("net", 0), 0)   # only positive net
            pts = net * w.points_per_net_buy
            raw += pts
            breakdown["net_buy_points"] = round(pts, 2)
        else:
            breakdown["net_buy_points"] = 0.0

        # --- Signal C: sector momentum bonus ---
        sector = signals.get("SECTOR_SURGE")
        if sector and sector.active:
            raw += w.sector_momentum_bonus
            breakdown["sector_momentum_points"] = w.sector_momentum_bonus
        else:
            breakdown["sector_momentum_points"] = 0.0

        # --- Signal D: repeat purchase bonus ---
        repeat = signals.get("REPEAT_BUYING")
        if repeat and repeat.active:
            raw += w.repeat_purchase_bonus
            breakdown["repeat_purchase_points"] = w.repeat_purchase_bonus
        else:
            breakdown["repeat_purchase_points"] = 0.0

        # --- Signal E: frequency multiplier (applied after raw sum) ---
        freq = signals.get("FREQUENCY_RISE")
        freq_active = freq is not None and freq.active
        breakdown["frequency_multiplier_active"] = 1.0 if freq_active else 0.0

        # --- Signal F: large transaction bonus ---
        large = signals.get("LARGE_TRANSACTION")
        if large and large.active:
            raw += w.large_transaction_bonus
            breakdown["large_transaction_points"] = w.large_transaction_bonus
        else:
            breakdown["large_transaction_points"] = 0.0

        # Apply frequency multiplier
        if freq_active:
            raw *= 1.25

        # Cap at max_raw_score
        raw = min(raw, w.max_raw_score)
        breakdown["active_signals"] = [s.signal_name for s in signals.active_signals]

        return raw, breakdown

    @staticmethod
    def _normalise(raw: float) -> float:
        """Normalise raw score to [0, 100]."""
        max_raw = SCORING_WEIGHTS.max_raw_score
        if max_raw <= 0:
            return 0.0
        normalised = (raw / max_raw) * 100.0
        return round(min(max(normalised, 0.0), 100.0), 2)

    def _persist_score(self, ticker: str, score: float, breakdown: dict) -> None:
        """Insert a new Score row into the database, ensuring Company row exists first."""
        from models import Company
        with get_session() as session:
            # Ensure a Company row exists — required by the foreign key constraint
            if session.get(Company, ticker) is None:
                # Look up a display name from the trades table
                from models import Trade
                from sqlalchemy import select
                trade = session.scalars(
                    select(Trade).where(Trade.ticker == ticker).limit(1)
                ).first()
                company_name = (trade.company_name if trade else None) or ticker
                session.add(Company(
                    ticker=ticker,
                    company_name=company_name,
                    ticker_verified=False,
                    manually_overridden=False,
                ))
                session.flush()

            row = Score(
                ticker=ticker,
                score=score,
                calculated_at=datetime.utcnow(),
            )
            row.breakdown = breakdown
            session.add(row)

    # ------------------------------------------------------------------ #
    # Watchlist implementations
    # ------------------------------------------------------------------ #

    def _watchlist_top_congressional(self, limit: int) -> list[WatchlistEntry]:
        """Highest current scores across all tickers (one row per ticker)."""
        # Tickers to exclude — non-equity instruments
        EXCLUDE_PATTERNS = ("CORPORATE BOND", "BOND", "ADVISOR-DRIVEN", "QUARTERLY", "DIVIDEND REINVESTMENT", "REINVESTMENT", "MUTUAL FUND", "ETF NOTE")

        with get_session() as session:
            # Get the single latest score per ticker using a correlated subquery
            latest_subq = (
                select(func.max(Score.calculated_at))
                .where(Score.ticker == Score.ticker)
                .correlate(Score)
                .scalar_subquery()
            )

            # Fetch one row per ticker: the most recent score
            all_scores = session.execute(
                select(Score.ticker, Score.score, Score.breakdown_json, Score.calculated_at)
            ).all()

            # Deduplicate in Python: keep only the latest score per ticker
            seen: dict[str, tuple] = {}
            for row in all_scores:
                if row.ticker not in seen or row.calculated_at > seen[row.ticker][3]:
                    seen[row.ticker] = row

            # Fetch company info
            tickers = list(seen.keys())
            companies = {
                c.ticker: c
                for c in session.scalars(
                    select(Company).where(Company.ticker.in_(tickers))
                ).all()
            }

        # Filter out non-stock instruments and sort by score
        results = []
        for ticker, row in seen.items():
            company = companies.get(ticker)
            company_name = company.company_name if company else ticker

            # Skip bonds and non-equity instruments
            if any(p in company_name.upper() for p in EXCLUDE_PATTERNS):
                continue
            # Skip tickers that look like CUSIP numbers (9 chars, alphanumeric)
            if len(ticker) == 9 and ticker.isalnum():
                continue

            bd = json.loads(row.breakdown_json or "{}")
            results.append((ticker, company_name, company, row.score, row.calculated_at, bd))

        results.sort(key=lambda x: x[3], reverse=True)

        entries = []
        for rank, (ticker, company_name, company, score, calc_at, bd) in enumerate(results[:limit], start=1):
            entries.append(
                WatchlistEntry(
                    ticker=ticker,
                    company_name=company_name,
                    sector=company.sector if company else None,
                    score=score,
                    rank=rank,
                    metadata={
                        "calculated_at": calc_at.isoformat() if calc_at else None,
                        "active_signals": bd.get("active_signals", []),
                    },
                )
            )
        return entries

    def _watchlist_top_sector(self, limit: int) -> list[WatchlistEntry]:
        """Sectors ranked by aggregate score of their top 5 tickers."""
        top = self._watchlist_top_congressional(50)

        # Aggregate by sector
        sector_scores: dict[str, list[float]] = {}
        sector_tickers: dict[str, list[str]] = {}
        for entry in top:
            sector = entry.sector or "Unknown"
            sector_scores.setdefault(sector, []).append(entry.score)
            sector_tickers.setdefault(sector, []).append(entry.ticker)

        ranked: list[tuple[str, float]] = []
        for sector, scores in sector_scores.items():
            avg = sum(scores) / len(scores)
            ranked.append((sector, avg))

        ranked.sort(key=lambda x: x[1], reverse=True)

        entries = []
        for rank, (sector, avg_score) in enumerate(ranked[:limit], start=1):
            entries.append(
                WatchlistEntry(
                    ticker="",
                    company_name=sector,
                    sector=sector,
                    score=round(avg_score, 2),
                    rank=rank,
                    metadata={
                        "tickers": sector_tickers.get(sector, [])[:5],
                        "ticker_count": len(sector_tickers.get(sector, [])),
                    },
                )
            )
        return entries

    def _watchlist_most_bought(self, limit: int) -> list[WatchlistEntry]:
        """Tickers with the most congressional buy events in the last 30 days."""
        cutoff = datetime.utcnow() - timedelta(days=30)

        with get_session() as session:
            from sqlalchemy import func as sqlfunc

            stmt = (
                select(
                    Trade.ticker,
                    sqlfunc.count(Trade.id).label("buy_count"),
                    sqlfunc.count(Trade.politician_name.distinct()).label("unique_buyers"),
                )
                .where(
                    Trade.ticker.isnot(None),
                    Trade.trade_type == "purchase",
                    Trade.trade_date >= cutoff,
                )
                .group_by(Trade.ticker)
                .order_by(desc("buy_count"))
                .limit(limit)
            )
            rows = session.execute(stmt).all()

            # Fetch company info for these tickers
            tickers = [r.ticker for r in rows]
            companies = {
                c.ticker: c
                for c in session.scalars(
                    select(Company).where(Company.ticker.in_(tickers))
                ).all()
            }

        entries = []
        for rank, row in enumerate(rows, start=1):
            company = companies.get(row.ticker)
            latest = self.get_latest_score(row.ticker)
            entries.append(
                WatchlistEntry(
                    ticker=row.ticker,
                    company_name=(company.company_name if company else row.ticker),
                    sector=(company.sector if company else None),
                    score=latest.score if latest else 0.0,
                    rank=rank,
                    metadata={
                        "buy_count": row.buy_count,
                        "unique_buyers": row.unique_buyers,
                    },
                )
            )
        return entries

    def _watchlist_fastest_rising(self, limit: int) -> list[WatchlistEntry]:
        """
        Tickers whose score increased most rapidly (points/day) in the
        last 7 days relative to 7–14 days ago.
        """
        now = datetime.utcnow()
        recent_cutoff = now - timedelta(days=7)
        prior_cutoff = now - timedelta(days=14)

        with get_session() as session:
            # Most recent score per ticker in each window
            def latest_in_window(after: datetime, before: datetime):
                subq = (
                    select(Score.ticker, Score.score, Score.calculated_at)
                    .where(Score.calculated_at >= after, Score.calculated_at < before)
                    .distinct(Score.ticker)
                    .order_by(Score.ticker, desc(Score.calculated_at))
                    .subquery()
                )
                return {
                    row.ticker: row.score
                    for row in session.execute(select(subq)).all()
                }

            recent_scores = latest_in_window(recent_cutoff, now)
            prior_scores = latest_in_window(prior_cutoff, recent_cutoff)

            tickers_with_both = set(recent_scores) & set(prior_scores)
            deltas = {
                t: recent_scores[t] - prior_scores[t]
                for t in tickers_with_both
            }
            top_tickers = sorted(deltas, key=lambda t: deltas[t], reverse=True)[:limit]

            companies = {
                c.ticker: c
                for c in session.scalars(
                    select(Company).where(Company.ticker.in_(top_tickers))
                ).all()
            }

        entries = []
        for rank, ticker in enumerate(top_tickers, start=1):
            company = companies.get(ticker)
            entries.append(
                WatchlistEntry(
                    ticker=ticker,
                    company_name=(company.company_name if company else ticker),
                    sector=(company.sector if company else None),
                    score=recent_scores[ticker],
                    rank=rank,
                    metadata={
                        "score_delta": round(deltas[ticker], 2),
                        "prior_score": round(prior_scores[ticker], 2),
                    },
                )
            )
        return entries

    def _watchlist_most_improved(self, limit: int) -> list[WatchlistEntry]:
        """
        Tickers with the largest absolute score gain over the last 7 days.
        Same logic as FASTEST_RISING but sorted by absolute delta.
        """
        # Reuse fastest rising but sort differently
        entries = self._watchlist_fastest_rising(limit=50)
        improved = sorted(
            entries,
            key=lambda e: e.metadata.get("score_delta", 0.0),
            reverse=True,
        )
        return [
            WatchlistEntry(
                ticker=e.ticker,
                company_name=e.company_name,
                sector=e.sector,
                score=e.score,
                rank=rank,
                metadata=e.metadata,
            )
            for rank, e in enumerate(improved[:limit], start=1)
        ]


# ---------------------------------------------------------------------------
# Watchlist summary helper (used by !watchlist command)
# ---------------------------------------------------------------------------

def build_watchlist_summary(engine: ScoringEngine) -> dict[str, list[WatchlistEntry]]:
    """
    Build all five watchlists in one call.

    Returns a dict keyed by watchlist name.
    """
    summary = {}
    for name in ScoringEngine.WATCHLIST_NAMES:
        try:
            summary[name] = engine.get_watchlist(name, limit=10)
        except Exception as exc:
            logger.error("[ScoringEngine] Failed to build watchlist {}: {}", name, exc)
            summary[name] = []
    return summary


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_scoring_instance: ScoringEngine | None = None


def get_scoring_engine() -> ScoringEngine:
    """Return the shared ScoringEngine singleton."""
    global _scoring_instance
    if _scoring_instance is None:
        _scoring_instance = ScoringEngine()
    return _scoring_instance
