"""
services/trade_collector.py — Trade data collection layer.

Architecture
------------
BaseCollector
    Abstract base class.  Subclasses implement _fetch_raw() and
    _parse_records().  The public collect() method handles retries,
    deduplication, amount-range parsing, and persistence.

CongressCollector(BaseCollector)
    Pulls congressional trade disclosures from the Quiver Quantitative
    REST API.  Falls back to a public House/Senate disclosure CSV mirror
    when no API key is configured.

Designed for future extension::

    class InsiderCollector(BaseCollector): ...
    class ThirteenFCollector(BaseCollector): ...
    class NewsCollector(BaseCollector): ...
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any

import requests
from loguru import logger
from sqlalchemy.exc import IntegrityError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import AMOUNT_RANGE_MAP, settings
from database import get_session
from models import Trade


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CollectorError(Exception):
    """Raised when a collector cannot fetch or parse data."""


class RateLimitError(CollectorError):
    """Raised when an API returns a 429 response."""


# ---------------------------------------------------------------------------
# HTTP session with retry baked in
# ---------------------------------------------------------------------------

def _build_http_session() -> requests.Session:
    """Return a requests.Session with common headers and a timeout adapter."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "CapitolGains-Bot/1.0 (github.com/your-org/capitol-gains)",
            "Accept": "application/json",
        }
    )
    return session


# ---------------------------------------------------------------------------
# Base collector
# ---------------------------------------------------------------------------

class BaseCollector(ABC):
    """
    Abstract base class for all data collectors.

    Subclasses must implement:
        _fetch_raw()       → list[dict]  (raw API/CSV rows)
        _parse_records()   → list[Trade] (mapped ORM objects, not yet saved)

    The public collect() method orchestrates:
        1. _fetch_raw()  with exponential-backoff retries
        2. _parse_records()
        3. Deduplication against the database
        4. Bulk insert of new trades
        5. Return count of new records inserted
    """

    # Subclasses can override these
    REQUEST_TIMEOUT: int = 30        # seconds
    RATE_LIMIT_DELAY: float = 1.0    # seconds between requests
    MAX_RETRIES: int = 3

    def __init__(self) -> None:
        self._http = _build_http_session()
        self.source_name: str = self.__class__.__name__

    # ------------------------------------------------------------------ #
    # Abstract interface
    # ------------------------------------------------------------------ #

    @abstractmethod
    def _fetch_raw(self) -> list[dict[str, Any]]:
        """
        Fetch raw records from the data source.

        Returns a list of dicts; exact keys are source-specific.
        Should raise CollectorError on unrecoverable failures.
        """

    @abstractmethod
    def _parse_records(self, raw: list[dict[str, Any]]) -> list[Trade]:
        """
        Map raw dicts to Trade ORM objects.

        Objects are NOT added to a session here — just constructed.
        Missing or unparseable fields should be logged and skipped
        rather than raising exceptions.
        """

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def collect(self) -> int:
        """
        Run a full collection cycle.

        Returns the number of new Trade rows inserted.
        Raises CollectorError if the fetch step fails after all retries.
        """
        logger.info("[{}] Starting collection cycle.", self.source_name)
        start = time.monotonic()

        try:
            raw = self._fetch_with_retry()
        except Exception as exc:
            raise CollectorError(
                f"[{self.source_name}] Fetch failed after {self.MAX_RETRIES} attempts: {exc}"
            ) from exc

        logger.info("[{}] Fetched {} raw records.", self.source_name, len(raw))

        trades = self._parse_records(raw)
        logger.info("[{}] Parsed {} Trade objects.", self.source_name, len(trades))

        inserted = self._persist(trades)
        elapsed = time.monotonic() - start

        logger.info(
            "[{}] Collection complete — {} new trades inserted in {:.1f}s.",
            self.source_name,
            inserted,
            elapsed,
        )
        return inserted

    # ------------------------------------------------------------------ #
    # Retry wrapper
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def _fetch_with_retry(self) -> list[dict[str, Any]]:
        return self._fetch_raw()

    # ------------------------------------------------------------------ #
    # Persistence with deduplication
    # ------------------------------------------------------------------ #

    def _persist(self, trades: list[Trade]) -> int:
        """
        Insert new trades into the database.

        Uses the unique constraint (politician_name, ticker, trade_date,
        trade_type) to silently skip duplicates rather than failing.
        Returns count of rows actually inserted.
        """
        if not trades:
            return 0

        inserted = 0
        with get_session() as session:
            for trade in trades:
                try:
                    session.add(trade)
                    session.flush()   # sends INSERT; triggers constraint check
                    inserted += 1
                except IntegrityError:
                    session.rollback()
                    logger.debug(
                        "[{}] Duplicate skipped: {} {} {} {}",
                        self.source_name,
                        trade.politician_name,
                        trade.ticker,
                        trade.trade_date,
                        trade.trade_type,
                    )
                except Exception as exc:
                    session.rollback()
                    logger.error(
                        "[{}] Failed to insert trade {}: {}",
                        self.source_name,
                        trade,
                        exc,
                    )

        return inserted

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_amount_range(range_str: str | None) -> tuple[float | None, float | None]:
        """
        Convert a STOCK Act amount range string to (min, max) floats.

        Returns (None, None) if the string is missing or unrecognised.
        """
        if not range_str:
            return None, None

        # Exact match first
        if range_str in AMOUNT_RANGE_MAP:
            return AMOUNT_RANGE_MAP[range_str]

        # Fuzzy: try each key as a substring
        normalised = range_str.strip()
        for key, bounds in AMOUNT_RANGE_MAP.items():
            if key.replace(",", "").replace(" ", "") in normalised.replace(",", "").replace(" ", ""):
                return bounds

        logger.warning("Unrecognised amount range: {!r}", range_str)
        return None, None

    @staticmethod
    def parse_date(date_str: str | None, formats: list[str] | None = None) -> datetime | None:
        """
        Try a list of date format strings and return the first that parses.

        Returns None if date_str is empty or no format matches.
        """
        if not date_str:
            return None

        _formats = formats or [
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%d/%m/%Y",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
        ]
        for fmt in _formats:
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue

        logger.warning("Could not parse date string: {!r}", date_str)
        return None

    def _rate_limit(self) -> None:
        """Sleep to respect API rate limits between paginated requests."""
        time.sleep(self.RATE_LIMIT_DELAY)


# ---------------------------------------------------------------------------
# Congress collector — Quiver Quantitative
# ---------------------------------------------------------------------------

class CongressCollector(BaseCollector):
    """
    Fetches congressional stock trade disclosures.

    Primary source: Quiver Quantitative API
        https://api.quiverquant.com/beta/live/congresstrading

    Fallback source: Senate/House public disclosure CSV
        Used automatically when QUIVER_API_KEY is not set.

    The Quiver endpoint returns trades from the last 6 months by default.
    We filter to the last LOOKBACK_DAYS to keep collection fast on
    subsequent runs while still catching delayed disclosures.
    """

    LOOKBACK_DAYS: int = 90        # 90 days — stable window that works reliably
    RATE_LIMIT_DELAY: float = 1.5  # Quiver rate limit

    # Quiver field → our field
    _QUIVER_FIELD_MAP: dict[str, str] = {
        "Name":          "politician_name",
        "Ticker":        "ticker",
        "Transaction":   "trade_type",
        "TransactionDate": "trade_date",
        "ReportDate":    "disclosure_date",
        "Range":         "amount_range",
        "House":         "politician_chamber",
        "Party":         "politician_party",
    }

    # Transaction values → our normalised values (covers Quiver + FMP formats)
    _TRADE_TYPE_MAP: dict[str, str] = {
        # Quiver format
        "Purchase":       "purchase",
        "Sale":           "sale",
        "Sale (Full)":    "sale",
        "Sale (Partial)": "sale",
        "Exchange":       "exchange",
        # FMP format
        "buy":            "purchase",
        "sell":           "sale",
        "purchase":       "purchase",
        "sale":           "sale",
        "sale_full":      "sale",
        "sale_partial":   "sale",
        "P":              "purchase",
        "S":              "sale",
        "S (partial)":    "sale",
        "S (Full)":       "sale",
    }

    def __init__(self) -> None:
        super().__init__()
        self._api_key = settings.quiver_api_key
        self._use_fallback = not self._api_key

        if self._use_fallback:
            logger.warning(
                "[CongressCollector] No QUIVER_API_KEY set — using public CSV fallback."
            )

    # ------------------------------------------------------------------ #
    # Fetch
    # ------------------------------------------------------------------ #

    def _fetch_raw(self) -> list[dict[str, Any]]:
        if self._use_fallback:
            return self._fetch_public_csv()
        return self._fetch_quiver()

    def _fetch_quiver(self) -> list[dict[str, Any]]:
        """
        Pull from the Quiver Quantitative congressional trading endpoint.

        Quiver returns up to 500 records per page; we paginate until
        we've received all records within LOOKBACK_DAYS.
        """
        base_url = "https://api.quiverquant.com/beta/live/congresstrading"
        headers = {
            "accept": "application/json",
            "X-CSRFToken": self._api_key,
            "Authorization": f"Token {self._api_key}",
        }
        cutoff = datetime.utcnow() - timedelta(days=self.LOOKBACK_DAYS)
        all_records: list[dict] = []
        page = 1

        while True:
            logger.debug("[CongressCollector] Fetching Quiver page {}.", page)
            try:
                response = self._http.get(
                    base_url,
                    headers=headers,
                    params={"page": page, "page_size": 100},
                    timeout=self.REQUEST_TIMEOUT,
                )
            except requests.Timeout:
                logger.error("[CongressCollector] Quiver request timed out on page {}.", page)
                raise

            if response.status_code == 429:
                raise RateLimitError("Quiver API rate limit hit.")
            if response.status_code == 401:
                raise CollectorError(
                    "Quiver API authentication failed — check QUIVER_API_KEY."
                )
            if response.status_code != 200:
                raise CollectorError(
                    f"Quiver API returned HTTP {response.status_code}: {response.text[:200]}"
                )

            page_data: list[dict] = response.json()
            if not page_data:
                break

            # Filter to lookback window based on disclosure date
            relevant = []
            for record in page_data:
                report_date = self.parse_date(record.get("ReportDate"))
                trade_date = self.parse_date(record.get("TransactionDate"))
                effective_date = report_date or trade_date
                if effective_date and effective_date >= cutoff:
                    relevant.append(record)

            all_records.extend(relevant)

            # If the last record on this page pre-dates our cutoff, stop paging
            last_record_date = self.parse_date(page_data[-1].get("ReportDate"))
            if last_record_date and last_record_date < cutoff:
                break

            if len(page_data) < 100:
                break  # last page

            page += 1
            self._rate_limit()

        logger.info("[CongressCollector] Retrieved {} records from Quiver.", len(all_records))
        return all_records

    def _fetch_public_csv(self) -> list[dict[str, Any]]:
        """
        Fallback: fetch congressional trades from Financial Modeling Prep (FMP).

        FMP provides both Senate and House trading data via their free API tier.
        Combines Senate + House results and normalises to the standard field format.
        """
        from config import settings as _settings
        fmp_key = _settings.fmp_api_key

        if not fmp_key:
            logger.error(
                "[CongressCollector] No FMP_API_KEY set. "
                "Add FMP_API_KEY to .env — free key at financialmodelingprep.com"
            )
            return []

        logger.info("[CongressCollector] Fetching from Financial Modeling Prep API.")

        all_records: list[dict] = []

        # FMP provides separate senate and house endpoints
        # We fetch the most active tickers to get a broad dataset
        endpoints = [
            ("Senate", "https://financialmodelingprep.com/api/v4/senate-trading-rss-feed"),
            ("House",  "https://financialmodelingprep.com/api/v4/house-disclosure-rss-feed"),
        ]

        for chamber, url in endpoints:
            try:
                response = self._http.get(
                    url,
                    params={"apikey": fmp_key, "page": 0},
                    timeout=self.REQUEST_TIMEOUT,
                )
                if response.status_code == 401:
                    raise CollectorError(
                        "FMP API authentication failed — check FMP_API_KEY in .env"
                    )
                if response.status_code == 403:
                    logger.warning(
                        "[CongressCollector] FMP {} endpoint returned 403 — "
                        "may need a paid plan for this endpoint.", chamber
                    )
                    continue
                if response.status_code != 200:
                    logger.warning(
                        "[CongressCollector] FMP {} returned HTTP {}.",
                        chamber, response.status_code
                    )
                    continue

                data = response.json()
                if not isinstance(data, list):
                    logger.warning("[CongressCollector] FMP {} unexpected format.", chamber)
                    continue

                logger.info(
                    "[CongressCollector] FMP {} returned {} records.", chamber, len(data)
                )

                # Normalise FMP fields to our standard format
                for r in data:
                    all_records.append({
                        "Name":            r.get("senator") or r.get("representative") or r.get("name") or "Unknown",
                        "Ticker":          (r.get("ticker") or r.get("symbol") or "").upper(),
                        "Transaction":     r.get("type") or r.get("transactionType") or r.get("transaction") or "",
                        "TransactionDate": r.get("transactionDate") or r.get("date") or "",
                        "ReportDate":      r.get("dateRecieved") or r.get("disclosureDate") or r.get("transactionDate") or "",
                        "Range":           r.get("amount") or r.get("range") or "",
                        "House":           chamber,
                        "Party":           r.get("party") or "",
                        "Company":         r.get("assetDescription") or r.get("company") or "",
                    })

                self._rate_limit()

            except CollectorError:
                raise
            except Exception as exc:
                logger.error(
                    "[CongressCollector] FMP {} fetch failed: {}", chamber, exc
                )

        if not all_records:
            logger.warning(
                "[CongressCollector] FMP returned no records. "
                "Check your FMP_API_KEY and plan tier."
            )

        return all_records

    # ------------------------------------------------------------------ #
    # Parse
    # ------------------------------------------------------------------ #

    def _parse_records(self, raw: list[dict[str, Any]]) -> list[Trade]:
        """Map Quiver API dicts to Trade ORM objects."""
        trades: list[Trade] = []
        skipped = 0

        for row in raw:
            try:
                trade = self._parse_single(row)
                if trade is not None:
                    trades.append(trade)
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning(
                    "[CongressCollector] Skipping unparseable row {!r}: {}",
                    row,
                    exc,
                )
                skipped += 1

        if skipped:
            logger.warning(
                "[CongressCollector] Skipped {} unparseable records.", skipped
            )

        return trades

    def _parse_single(self, row: dict[str, Any]) -> Trade | None:
        """
        Parse a single Quiver row dict into a Trade object.

        Returns None if mandatory fields (politician, trade_date, trade_type)
        cannot be resolved.
        """
        # Quiver uses "Representative" field; normalised fallback covers future sources
        politician = (
            row.get("Representative") or row.get("Name") or ""
        ).strip()
        if not politician:
            logger.debug("[CongressCollector] Row missing politician name, skipping.")
            return None

        trade_date = self.parse_date(row.get("TransactionDate"))
        if trade_date is None:
            logger.debug(
                "[CongressCollector] Could not parse TransactionDate for {}, skipping.",
                politician,
            )
            return None

        raw_type = (row.get("Transaction") or "").strip()
        trade_type = self._TRADE_TYPE_MAP.get(raw_type, raw_type.lower() or "unknown")
        if trade_type == "unknown":
            logger.debug(
                "[CongressCollector] Unknown trade type {!r} for {}, skipping.",
                raw_type,
                politician,
            )
            return None

        ticker = (row.get("Ticker") or "").strip().upper() or None
        # Quiver provides Description for company name
        company_name = (
            row.get("Description") or row.get("Company") or ticker or "Unknown"
        ).strip()
        amount_range = (row.get("Range") or "").strip() or None
        amount_min, amount_max = self.parse_amount_range(amount_range)

        # Map Quiver party codes to full names
        party_map = {"R": "Republican", "D": "Democrat", "I": "Independent"}
        raw_party = (row.get("Party") or "").strip()
        party = party_map.get(raw_party, raw_party) or None

        # Map Quiver chamber values
        chamber_map = {"Representatives": "House", "Senate": "Senate"}
        raw_chamber = (row.get("House") or "").strip()
        chamber = chamber_map.get(raw_chamber, raw_chamber) or None

        import json as _json
        return Trade(
            politician_name=politician,
            politician_party=party,
            politician_chamber=chamber,
            company_name=company_name,
            ticker=ticker,
            trade_type=trade_type,
            trade_date=trade_date,
            disclosure_date=self.parse_date(row.get("ReportDate")),
            amount_range=amount_range,
            amount_min=amount_min,
            amount_max=amount_max,
            source_url="https://api.quiverquant.com/beta/live/congresstrading",
            raw_data=_json.dumps(row),
        )


# ---------------------------------------------------------------------------
# Collector registry — makes it easy to run all collectors in the scheduler
# ---------------------------------------------------------------------------

COLLECTOR_REGISTRY: dict[str, type[BaseCollector]] = {
    "congress": CongressCollector,
    # Future collectors registered here:
    # "insider":   InsiderCollector,
    # "13f":       ThirteenFCollector,
    # "news":      NewsCollector,
}


def run_all_collectors() -> dict[str, int]:
    """
    Instantiate and run every registered collector.

    Returns a dict mapping collector name → records inserted.
    Failures in one collector do not prevent others from running.
    """
    results: dict[str, int] = {}

    for name, cls in COLLECTOR_REGISTRY.items():
        try:
            collector = cls()
            count = collector.collect()
            results[name] = count
            logger.info("Collector '{}' inserted {} new records.", name, count)
        except CollectorError as exc:
            logger.error("Collector '{}' failed: {}", name, exc)
            results[name] = 0
        except Exception as exc:
            logger.exception("Collector '{}' raised unexpected error: {}", name, exc)
            results[name] = 0

    return results
