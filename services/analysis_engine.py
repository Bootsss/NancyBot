"""
services/analysis_engine.py — Signal generation engine.

Analyses the trades table and produces structured signal dicts that
feed directly into the scoring engine and alert service.

Signals implemented
-------------------
Signal A — BUY_CLUSTER       3+ unique politicians buy same stock ≤30 days
Signal B — NET_BUY_ACTIVITY  Buy count exceeds sell count
Signal C — SECTOR_SURGE      Unusual concentration of buys in one sector
Signal D — REPEAT_BUYING     Same politician buys same stock multiple times
Signal E — FREQUENCY_RISE    Purchase frequency accelerating over time
Signal F — LARGE_TRANSACTION High-value transactions dominating activity

Each signal method returns a SignalResult dataclass containing the raw
data, a human-readable summary, and a boolean indicating whether the
signal is active (i.e. the threshold was met).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import and_, func, select

from config import settings
from database import get_session
from models import Company, Trade


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    """Outcome of a single signal check for one ticker."""

    signal_name: str
    ticker: str
    active: bool                          # did the signal threshold fire?
    value: float = 0.0                    # quantitative measure (e.g. buyer count)
    threshold: float = 0.0               # what value triggers the signal
    summary: str = ""                     # human-readable description
    data: dict = field(default_factory=dict)  # raw supporting data


@dataclass
class TickerSignals:
    """All signal results for a single ticker."""

    ticker: str
    signals: list[SignalResult] = field(default_factory=list)
    analysed_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def active_signals(self) -> list[SignalResult]:
        return [s for s in self.signals if s.active]

    @property
    def active_signal_names(self) -> list[str]:
        return [s.signal_name for s in self.active_signals]

    def get(self, signal_name: str) -> SignalResult | None:
        for s in self.signals:
            if s.signal_name == signal_name:
                return s
        return None


# ---------------------------------------------------------------------------
# Analysis engine
# ---------------------------------------------------------------------------

class AnalysisEngine:
    """
    Runs all six signals against the trades table.

    Usage::

        engine = AnalysisEngine()

        # Analyse one ticker
        result: TickerSignals = engine.analyse_ticker("AAPL")

        # Analyse all tickers traded in the last N days
        all_results = engine.analyse_all(lookback_days=90)
    """

    def __init__(self) -> None:
        self.cluster_window_days: int = settings.buy_cluster_window_days
        self.cluster_min_politicians: int = settings.buy_cluster_min_politicians

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def analyse_ticker(self, ticker: str, lookback_days: int = 90) -> TickerSignals:
        """
        Run all six signals for a single ticker.

        Returns a TickerSignals object with one SignalResult per signal.
        """
        logger.debug("[AnalysisEngine] Analysing ticker: {}", ticker)
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)

        trades = self._load_trades(ticker, cutoff)
        if not trades:
            logger.debug("[AnalysisEngine] No trades found for {} in last {} days.", ticker, lookback_days)

        result = TickerSignals(ticker=ticker)
        result.signals = [
            self.signal_a_buy_cluster(ticker, trades),
            self.signal_b_net_buy_activity(ticker, trades),
            self.signal_c_sector_surge(ticker, lookback_days),
            self.signal_d_repeat_buying(ticker, trades),
            self.signal_e_frequency_rise(ticker, lookback_days),
            self.signal_f_large_transaction(ticker, trades),
        ]

        active_count = len(result.active_signals)
        logger.debug(
            "[AnalysisEngine] {} — {}/{} signals active: {}",
            ticker,
            active_count,
            len(result.signals),
            result.active_signal_names,
        )
        return result

    def analyse_all(self, lookback_days: int = 90) -> list[TickerSignals]:
        """
        Run analysis for every ticker that has trades in the lookback window.

        Returns a list of TickerSignals sorted by active signal count descending.
        """
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        tickers = self._get_active_tickers(cutoff)

        logger.info(
            "[AnalysisEngine] Analysing {} tickers (last {} days).",
            len(tickers),
            lookback_days,
        )

        results: list[TickerSignals] = []
        for ticker in tickers:
            try:
                ts = self.analyse_ticker(ticker, lookback_days)
                results.append(ts)
            except Exception as exc:
                logger.error(
                    "[AnalysisEngine] Failed to analyse {}: {}", ticker, exc
                )

        results.sort(key=lambda r: len(r.active_signals), reverse=True)
        logger.info(
            "[AnalysisEngine] Analysis complete — {} tickers processed.", len(results)
        )
        return results

    # ------------------------------------------------------------------ #
    # Signal A — Buy Cluster
    # ------------------------------------------------------------------ #

    def signal_a_buy_cluster(
        self, ticker: str, trades: list[Trade]
    ) -> SignalResult:
        """
        Signal A: 3+ unique politicians buy the same stock within 30 days.

        Finds the maximum number of unique buyers in any rolling 30-day
        window within the trade list.
        """
        buys = [t for t in trades if t.is_buy]
        threshold = float(self.cluster_min_politicians)

        if len(buys) < self.cluster_min_politicians:
            return SignalResult(
                signal_name="BUY_CLUSTER",
                ticker=ticker,
                active=False,
                value=len(buys),
                threshold=threshold,
                summary=f"Only {len(buys)} buy(s) found — cluster threshold not met.",
                data={"buy_count": len(buys), "unique_buyers": []},
            )

        # Slide a 30-day window across sorted buy dates
        buys_sorted = sorted(buys, key=lambda t: t.trade_date)
        window_days = timedelta(days=self.cluster_window_days)
        max_unique_buyers: int = 0
        best_window_buyers: list[str] = []
        best_window_start: datetime | None = None

        for i, anchor in enumerate(buys_sorted):
            window_end = anchor.trade_date + window_days
            window_trades = [
                t for t in buys_sorted[i:]
                if t.trade_date <= window_end
            ]
            unique_buyers = list({t.politician_name for t in window_trades})
            if len(unique_buyers) > max_unique_buyers:
                max_unique_buyers = len(unique_buyers)
                best_window_buyers = unique_buyers
                best_window_start = anchor.trade_date

        active = max_unique_buyers >= self.cluster_min_politicians

        if active:
            summary = (
                f"{max_unique_buyers} politicians bought ${ticker} within "
                f"{self.cluster_window_days} days "
                f"(starting {best_window_start.strftime('%Y-%m-%d') if best_window_start else 'N/A'})."
            )
        else:
            summary = (
                f"Peak unique buyers in any {self.cluster_window_days}-day window: "
                f"{max_unique_buyers} (threshold: {self.cluster_min_politicians})."
            )

        return SignalResult(
            signal_name="BUY_CLUSTER",
            ticker=ticker,
            active=active,
            value=float(max_unique_buyers),
            threshold=threshold,
            summary=summary,
            data={
                "max_unique_buyers": max_unique_buyers,
                "buyers": best_window_buyers,
                "window_start": best_window_start.isoformat() if best_window_start else None,
                "window_days": self.cluster_window_days,
            },
        )

    # ------------------------------------------------------------------ #
    # Signal B — Net Buy Activity
    # ------------------------------------------------------------------ #

    def signal_b_net_buy_activity(
        self, ticker: str, trades: list[Trade]
    ) -> SignalResult:
        """
        Signal B: Buy count exceeds sell count over the lookback window.

        Also computes net buy ratio: buys / (buys + sells).
        """
        buys = [t for t in trades if t.is_buy]
        sells = [t for t in trades if t.is_sell]
        buy_count = len(buys)
        sell_count = len(sells)
        total = buy_count + sell_count

        net = buy_count - sell_count
        ratio = buy_count / total if total > 0 else 0.0
        active = buy_count > sell_count and buy_count >= 2

        summary = (
            f"{buy_count} buys vs {sell_count} sells "
            f"(net: {net:+d}, ratio: {ratio:.0%})."
        )

        return SignalResult(
            signal_name="NET_BUY_ACTIVITY",
            ticker=ticker,
            active=active,
            value=float(net),
            threshold=0.0,
            summary=summary,
            data={
                "buy_count": buy_count,
                "sell_count": sell_count,
                "net": net,
                "buy_ratio": round(ratio, 4),
                "buyers": list({t.politician_name for t in buys}),
                "sellers": list({t.politician_name for t in sells}),
            },
        )

    # ------------------------------------------------------------------ #
    # Signal C — Sector Surge
    # ------------------------------------------------------------------ #

    def signal_c_sector_surge(
        self, ticker: str, lookback_days: int = 90
    ) -> SignalResult:
        """
        Signal C: Unusual concentration of congressional buys in the
        same sector as this ticker.

        Compares this sector's buy share to its historical average.
        Active if sector's current share is ≥1.5× its trailing average.
        """
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        sector = self._get_sector(ticker)

        if not sector:
            return SignalResult(
                signal_name="SECTOR_SURGE",
                ticker=ticker,
                active=False,
                value=0.0,
                threshold=1.5,
                summary="Sector unknown — cannot compute sector concentration.",
                data={"sector": None},
            )

        # Count buys by sector in the lookback window
        sector_counts = self._sector_buy_counts(cutoff)
        total_buys = sum(sector_counts.values())

        if total_buys == 0:
            return SignalResult(
                signal_name="SECTOR_SURGE",
                ticker=ticker,
                active=False,
                value=0.0,
                threshold=1.5,
                summary="No buy trades found in the lookback window.",
                data={"sector": sector, "sector_counts": {}},
            )

        this_sector_buys = sector_counts.get(sector, 0)
        this_sector_share = this_sector_buys / total_buys
        num_sectors = len(sector_counts)
        expected_share = 1.0 / num_sectors if num_sectors > 0 else 0.0
        concentration_ratio = (
            this_sector_share / expected_share if expected_share > 0 else 0.0
        )

        active = concentration_ratio >= 1.5 and this_sector_buys >= 5

        summary = (
            f"{sector}: {this_sector_buys}/{total_buys} buys "
            f"({this_sector_share:.0%} share, {concentration_ratio:.1f}× expected)."
        )

        return SignalResult(
            signal_name="SECTOR_SURGE",
            ticker=ticker,
            active=active,
            value=concentration_ratio,
            threshold=1.5,
            summary=summary,
            data={
                "sector": sector,
                "sector_buys": this_sector_buys,
                "total_buys": total_buys,
                "sector_share": round(this_sector_share, 4),
                "concentration_ratio": round(concentration_ratio, 4),
                "all_sectors": sector_counts,
            },
        )

    # ------------------------------------------------------------------ #
    # Signal D — Repeat Buying
    # ------------------------------------------------------------------ #

    def signal_d_repeat_buying(
        self, ticker: str, trades: list[Trade]
    ) -> SignalResult:
        """
        Signal D: The same politician buys the same stock multiple times.

        Checks for any politician with ≥2 purchase events in the window.
        """
        buys = [t for t in trades if t.is_buy]

        # Count purchases per politician
        counts: dict[str, int] = {}
        for t in buys:
            counts[t.politician_name] = counts.get(t.politician_name, 0) + 1

        repeat_buyers = {name: cnt for name, cnt in counts.items() if cnt >= 2}
        active = len(repeat_buyers) > 0

        if active:
            top = sorted(repeat_buyers.items(), key=lambda x: x[1], reverse=True)
            top_name, top_count = top[0]
            summary = (
                f"{len(repeat_buyers)} politician(s) made repeat purchases. "
                f"Top: {top_name} ({top_count}×)."
            )
        else:
            summary = "No repeat purchases detected in the lookback window."

        return SignalResult(
            signal_name="REPEAT_BUYING",
            ticker=ticker,
            active=active,
            value=float(len(repeat_buyers)),
            threshold=1.0,
            summary=summary,
            data={
                "repeat_buyers": repeat_buyers,
                "total_buy_events": len(buys),
            },
        )

    # ------------------------------------------------------------------ #
    # Signal E — Frequency Rise
    # ------------------------------------------------------------------ #

    def signal_e_frequency_rise(
        self, ticker: str, lookback_days: int = 90
    ) -> SignalResult:
        """
        Signal E: Purchase frequency is accelerating over time.

        Compares trades-per-month in the first half vs second half of
        the lookback window.  Active if recent half has ≥2× the rate.
        """
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        mid = datetime.utcnow() - timedelta(days=lookback_days // 2)
        all_trades = self._load_trades(ticker, cutoff)

        buys = [t for t in all_trades if t.is_buy]
        early_buys = [t for t in buys if t.trade_date < mid]
        recent_buys = [t for t in buys if t.trade_date >= mid]

        half_days = lookback_days / 2
        early_rate = len(early_buys) / half_days * 30   # per month
        recent_rate = len(recent_buys) / half_days * 30

        acceleration = recent_rate / early_rate if early_rate > 0 else (
            float("inf") if recent_buys else 0.0
        )

        # Active only if there's meaningful recent volume
        active = (
            recent_rate > early_rate
            and len(recent_buys) >= 2
            and (acceleration >= 2.0 or (early_rate == 0 and len(recent_buys) >= 3))
        )

        if acceleration == float("inf"):
            accel_str = "∞ (no prior activity)"
        else:
            accel_str = f"{acceleration:.1f}×"

        summary = (
            f"Purchase rate: {early_rate:.1f}/mo (early) → "
            f"{recent_rate:.1f}/mo (recent). Acceleration: {accel_str}."
        )

        return SignalResult(
            signal_name="FREQUENCY_RISE",
            ticker=ticker,
            active=active,
            value=acceleration if acceleration != float("inf") else 99.0,
            threshold=2.0,
            summary=summary,
            data={
                "early_buy_count": len(early_buys),
                "recent_buy_count": len(recent_buys),
                "early_rate_per_month": round(early_rate, 2),
                "recent_rate_per_month": round(recent_rate, 2),
                "acceleration_ratio": round(acceleration, 2) if acceleration != float("inf") else None,
            },
        )

    # ------------------------------------------------------------------ #
    # Signal F — Large Transaction
    # ------------------------------------------------------------------ #

    def signal_f_large_transaction(
        self, ticker: str, trades: list[Trade]
    ) -> SignalResult:
        """
        Signal F: High-value transactions dominating the buy activity.

        A transaction is "large" if its midpoint is ≥$100,000.
        Active if ≥50% of buys by dollar value are in large-size buckets.
        """
        buys = [t for t in trades if t.is_buy]
        LARGE_THRESHOLD = 100_000.0

        if not buys:
            return SignalResult(
                signal_name="LARGE_TRANSACTION",
                ticker=ticker,
                active=False,
                value=0.0,
                threshold=50.0,
                summary="No buy trades in the lookback window.",
                data={"large_count": 0, "total_count": 0},
            )

        large_buys = [
            t for t in buys
            if (t.amount_midpoint or 0) >= LARGE_THRESHOLD
        ]
        large_share = len(large_buys) / len(buys) * 100
        active = large_share >= 50.0 and len(large_buys) >= 2

        total_estimated = sum(t.amount_midpoint or 0 for t in buys)
        large_estimated = sum(t.amount_midpoint or 0 for t in large_buys)

        summary = (
            f"{len(large_buys)}/{len(buys)} buys are large (≥$100K): "
            f"{large_share:.0f}% of transactions, "
            f"~${large_estimated:,.0f} of ~${total_estimated:,.0f} total."
        )

        return SignalResult(
            signal_name="LARGE_TRANSACTION",
            ticker=ticker,
            active=active,
            value=large_share,
            threshold=50.0,
            summary=summary,
            data={
                "large_count": len(large_buys),
                "total_count": len(buys),
                "large_share_pct": round(large_share, 2),
                "large_politicians": list({t.politician_name for t in large_buys}),
                "estimated_large_total": round(large_estimated, 0),
                "estimated_total": round(total_estimated, 0),
            },
        )

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #

    def _load_trades(self, ticker: str, cutoff: datetime) -> list[Trade]:
        """Load all trades for a ticker since the cutoff date."""
        with get_session() as session:
            stmt = (
                select(Trade)
                .where(
                    and_(
                        Trade.ticker == ticker,
                        Trade.trade_date >= cutoff,
                    )
                )
                .order_by(Trade.trade_date.asc())
            )
            return list(session.scalars(stmt).all())

    def _get_active_tickers(self, cutoff: datetime) -> list[str]:
        """Return distinct tickers that have trades since the cutoff."""
        with get_session() as session:
            stmt = (
                select(Trade.ticker)
                .where(
                    and_(
                        Trade.ticker.isnot(None),
                        Trade.trade_date >= cutoff,
                    )
                )
                .distinct()
            )
            return list(session.scalars(stmt).all())

    def _get_sector(self, ticker: str) -> str | None:
        """Return the sector for a ticker from the companies table."""
        with get_session() as session:
            company = session.get(Company, ticker)
            return company.sector if company else None

    def _sector_buy_counts(self, cutoff: datetime) -> dict[str, int]:
        """
        Count congressional purchases by sector since the cutoff.

        Returns {sector_name: buy_count}.
        """
        with get_session() as session:
            stmt = (
                select(Company.sector, func.count(Trade.id).label("cnt"))
                .join(Company, Trade.ticker == Company.ticker)
                .where(
                    and_(
                        Trade.trade_date >= cutoff,
                        Trade.trade_type == "purchase",
                        Company.sector.isnot(None),
                    )
                )
                .group_by(Company.sector)
            )
            rows = session.execute(stmt).all()
            return {row.sector: row.cnt for row in rows}


# ------------------------------------------------------------------ #
# Module-level singleton
# ------------------------------------------------------------------ #

_engine_instance: AnalysisEngine | None = None


def get_analysis_engine() -> AnalysisEngine:
    """Return the shared AnalysisEngine singleton."""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AnalysisEngine()
    return _engine_instance
