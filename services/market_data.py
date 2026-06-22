"""
services/market_data.py — Market data enrichment service.

Fetches and caches financial metrics for every ticker in the companies
table using yfinance.  Designed to be called by the weekly scheduler job.

Metrics stored per ticker
-------------------------
- current_price
- market_cap
- pe_ratio
- revenue_growth   (YoY %)
- week_52_high
- week_52_low
- week_52_return   (% price change over trailing 52 weeks)
- avg_volume       (30-day average daily volume)

All data is written back to the Company row in the database.
A separate price-history helper supports the backtesting module.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger
from sqlalchemy import select

from database import get_session
from models import Company


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RATE_LIMIT_DELAY: float = 0.5      # seconds between individual ticker calls
BATCH_SIZE: int = 20                # tickers per yfinance batch download
SPY_TICKER: str = "SPY"            # S&P 500 proxy for backtest comparisons


# ---------------------------------------------------------------------------
# MarketDataService
# ---------------------------------------------------------------------------

class MarketDataService:
    """
    Enriches Company rows with current market data from yfinance.

    Usage::

        svc = MarketDataService()
        svc.update_all()              # update every company in DB
        svc.update_ticker("AAPL")     # update a single ticker
        price = svc.get_price("AAPL") # current price (cached)
    """

    def __init__(self) -> None:
        # In-process price cache to avoid duplicate calls within one run
        self._price_cache: dict[str, float | None] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def update_all(self) -> dict[str, bool]:
        """
        Update market data for every Company row in the database.

        Returns a dict of ticker → success flag.
        """
        with get_session() as session:
            tickers: list[str] = list(
                session.scalars(select(Company.ticker)).all()
            )

        if not tickers:
            logger.info("[MarketData] No companies in database — nothing to update.")
            return {}

        logger.info("[MarketData] Updating market data for {} tickers.", len(tickers))
        results: dict[str, bool] = {}

        # Process in batches to reduce HTTP round-trips
        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i : i + BATCH_SIZE]
            batch_results = self._update_batch(batch)
            results.update(batch_results)
            if i + BATCH_SIZE < len(tickers):
                time.sleep(RATE_LIMIT_DELAY * 2)

        success = sum(1 for v in results.values() if v)
        logger.info(
            "[MarketData] Update complete — {}/{} tickers succeeded.",
            success,
            len(tickers),
        )
        return results

    def update_ticker(self, ticker: str) -> bool:
        """
        Fetch and persist market data for a single ticker.

        Returns True on success, False on failure.
        """
        info = self._fetch_info(ticker)
        if info is None:
            return False

        w52_return = self._calc_52w_return(ticker)
        avg_vol = self._calc_avg_volume(ticker)

        with get_session() as session:
            company = session.get(Company, ticker)
            if company is None:
                logger.warning(
                    "[MarketData] Ticker {} not found in companies table.", ticker
                )
                return False

            company.current_price = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
            company.market_cap = _safe_float(info.get("marketCap"))
            company.pe_ratio = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
            company.revenue_growth = _safe_float(info.get("revenueGrowth"))
            company.week_52_high = _safe_float(info.get("fiftyTwoWeekHigh"))
            company.week_52_low = _safe_float(info.get("fiftyTwoWeekLow"))
            company.week_52_return = w52_return
            company.avg_volume = avg_vol
            company.sector = info.get("sector") or company.sector
            company.industry = info.get("industry") or company.industry
            company.last_updated = datetime.utcnow()

            logger.debug(
                "[MarketData] Updated {}: price={} mktcap={} sector={}",
                ticker,
                company.current_price,
                company.market_cap,
                company.sector,
            )

        # Invalidate price cache for this ticker
        self._price_cache.pop(ticker, None)
        return True

    def get_price(self, ticker: str) -> float | None:
        """
        Return the current price for a ticker.

        Uses the in-process cache first, then yfinance fast_info.
        """
        if ticker in self._price_cache:
            return self._price_cache[ticker]

        try:
            t = yf.Ticker(ticker)
            price = (
                t.fast_info.get("lastPrice")
                or t.fast_info.get("regularMarketPrice")
            )
            price = _safe_float(price)
        except Exception as exc:
            logger.warning("[MarketData] get_price failed for {}: {}", ticker, exc)
            price = None

        self._price_cache[ticker] = price
        return price

    def get_historical_prices(
        self,
        ticker: str,
        start: datetime,
        end: datetime | None = None,
    ) -> pd.DataFrame | None:
        """
        Return a DataFrame of daily OHLCV data for the given date range.

        Columns: Open, High, Low, Close, Volume, Dividends, Stock Splits
        Index: DatetimeIndex (UTC)

        Returns None if the download fails or returns empty data.
        """
        end = end or datetime.utcnow()
        try:
            df = yf.download(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
            )
            if df.empty:
                logger.warning(
                    "[MarketData] No price history for {} ({} → {}).",
                    ticker, start.date(), end.date(),
                )
                return None
            return df
        except Exception as exc:
            logger.error(
                "[MarketData] Historical price download failed for {}: {}", ticker, exc
            )
            return None

    def get_return(
        self,
        ticker: str,
        from_date: datetime,
        to_date: datetime | None = None,
    ) -> float | None:
        """
        Calculate the percentage price return for ticker between two dates.

        Returns None if data is unavailable for either date.
        """
        to_date = to_date or datetime.utcnow()
        # Fetch a window slightly wider to handle weekends/holidays
        start = from_date - timedelta(days=5)
        end = to_date + timedelta(days=1)

        df = self.get_historical_prices(ticker, start, end)
        if df is None or df.empty:
            return None

        close = df["Close"].dropna()
        if len(close) < 2:
            return None

        # Find the closest trading day on or after from_date
        from_ts = pd.Timestamp(from_date.date())
        to_ts = pd.Timestamp(to_date.date())

        future_dates = close.index[close.index >= from_ts]
        past_dates = close.index[close.index <= to_ts]

        if future_dates.empty or past_dates.empty:
            return None

        entry_price = float(close.loc[future_dates[0]])
        exit_price = float(close.loc[past_dates[-1]])

        if entry_price == 0:
            return None

        return round((exit_price - entry_price) / entry_price * 100, 4)

    def get_spy_return(
        self,
        from_date: datetime,
        to_date: datetime | None = None,
    ) -> float | None:
        """Return SPY (S&P 500) percentage return over the given window."""
        return self.get_return(SPY_TICKER, from_date, to_date)

    # ------------------------------------------------------------------ #
    # Batch update
    # ------------------------------------------------------------------ #

    def _update_batch(self, tickers: list[str]) -> dict[str, bool]:
        """
        Fetch info for a batch of tickers and persist each one.

        Falls back to individual calls if the batch download fails.
        """
        results: dict[str, bool] = {}
        for ticker in tickers:
            try:
                success = self.update_ticker(ticker)
                results[ticker] = success
                time.sleep(RATE_LIMIT_DELAY)
            except Exception as exc:
                logger.error(
                    "[MarketData] Unexpected error updating {}: {}", ticker, exc
                )
                results[ticker] = False
        return results

    # ------------------------------------------------------------------ #
    # yfinance helpers
    # ------------------------------------------------------------------ #

    def _fetch_info(self, ticker: str) -> dict | None:
        """
        Fetch the yfinance .info dict for a ticker.

        Returns None on failure.  Logs the error but does not raise.
        """
        try:
            time.sleep(RATE_LIMIT_DELAY)
            info = yf.Ticker(ticker).info
            if not info or len(info) < 5:
                logger.warning(
                    "[MarketData] yfinance returned sparse info for {}.", ticker
                )
                return None
            return info
        except Exception as exc:
            logger.error(
                "[MarketData] yfinance info failed for {}: {}", ticker, exc
            )
            return None

    def _calc_52w_return(self, ticker: str) -> float | None:
        """
        Calculate trailing 52-week price return as a percentage.

        Uses yfinance download for accuracy over the .info snapshot values.
        """
        end = datetime.utcnow()
        start = end - timedelta(weeks=53)  # extra week buffer

        df = self.get_historical_prices(ticker, start, end)
        if df is None or len(df) < 2:
            return None

        close = df["Close"].dropna()
        if len(close) < 2:
            return None

        # Price ~52 weeks ago and today
        one_year_ago = pd.Timestamp((end - timedelta(weeks=52)).date())
        past_dates = close.index[close.index >= one_year_ago]

        if past_dates.empty:
            return None

        price_52w_ago = float(close.loc[past_dates[0]])
        price_now = float(close.iloc[-1])

        if price_52w_ago == 0:
            return None

        return round((price_now - price_52w_ago) / price_52w_ago * 100, 4)

    def _calc_avg_volume(self, ticker: str) -> float | None:
        """Calculate 30-day average daily trading volume."""
        end = datetime.utcnow()
        start = end - timedelta(days=35)

        df = self.get_historical_prices(ticker, start, end)
        if df is None or df.empty:
            return None

        vol = df["Volume"].dropna()
        if vol.empty:
            return None

        return round(float(vol.tail(30).mean()), 0)


# ---------------------------------------------------------------------------
# Summary statistics helpers (used by !stats command and reports)
# ---------------------------------------------------------------------------

def get_sector_summary(tickers: list[str]) -> dict[str, dict]:
    """
    Return a dict of sector → {count, tickers} for the given ticker list.

    Used by the !sector command and weekly sector-trend report.
    """
    summary: dict[str, dict] = {}

    with get_session() as session:
        stmt = select(Company).where(Company.ticker.in_(tickers))
        companies = session.scalars(stmt).all()

        for company in companies:
            sector = company.sector or "Unknown"
            if sector not in summary:
                summary[sector] = {"count": 0, "tickers": []}
            summary[sector]["count"] += 1
            summary[sector]["tickers"].append(company.ticker)

    return dict(sorted(summary.items(), key=lambda x: x[1]["count"], reverse=True))


def format_market_cap(market_cap: float | None) -> str:
    """Format a market cap float as a human-readable string."""
    if market_cap is None:
        return "N/A"
    if market_cap >= 1_000_000_000_000:
        return f"${market_cap / 1_000_000_000_000:.2f}T"
    if market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.2f}B"
    if market_cap >= 1_000_000:
        return f"${market_cap / 1_000_000:.2f}M"
    return f"${market_cap:,.0f}"


def format_volume(volume: float | None) -> str:
    """Format an average volume float as a human-readable string."""
    if volume is None:
        return "N/A"
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"{volume / 1_000:.1f}K"
    return f"{volume:,.0f}"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _safe_float(value) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        result = float(value)
        # yfinance sometimes returns Infinity for broken data
        if result != result or abs(result) == float("inf"):
            return None
        return result
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_market_data_instance: MarketDataService | None = None


def get_market_data_service() -> MarketDataService:
    """Return the shared MarketDataService singleton."""
    global _market_data_instance
    if _market_data_instance is None:
        _market_data_instance = MarketDataService()
    return _market_data_instance
