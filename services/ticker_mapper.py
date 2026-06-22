"""
services/ticker_mapper.py — Company name → ticker resolution service.

Resolution order
----------------
1. Manual overrides  (data/ticker_overrides.json)
2. Database cache    (companies table)
3. yfinance search   (live lookup)
4. Heuristic search  (strip legal suffixes, retry)

Results are cached in the companies table so subsequent runs never hit
the network for the same name.  Unmatched names are logged with enough
context to make manual overrides easy to write.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import yfinance as yf
from loguru import logger
from sqlalchemy import select

from config import settings
from database import get_session
from models import Company


# ---------------------------------------------------------------------------
# Legal-suffix patterns to strip before fuzzy matching
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES = re.compile(
    r"\b("
    r"Inc\.?|Incorporated|Corp\.?|Corporation|Co\.?|Company|"
    r"Ltd\.?|Limited|LLC|L\.L\.C\.|LP|L\.P\.|LLP|"
    r"PLC|plc|SE|AG|SA|N\.V\.|NV|BV|B\.V\.|"
    r"Holdings?|Group|Technologies|Technology|Tech|"
    r"Enterprises?|Solutions?|Systems?|Services?|"
    r"International|Global|Industries|Pharmaceuticals?|Pharma|"
    r"Bancorp|Bancshares|Banc|Financial|Finl|"
    r"Class\s+[A-Z]|Common\s+Stock"
    r")\b[.,]?",
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r"\s{2,}")


# ---------------------------------------------------------------------------
# Ticker mapper
# ---------------------------------------------------------------------------

class TickerMapper:
    """
    Resolves company names to ticker symbols and enriches the companies
    table with sector / industry metadata pulled from yfinance.

    All resolved tickers are cached so the network is only hit once per
    unique company name.

    Usage::

        mapper = TickerMapper()
        ticker = mapper.resolve("Apple Inc.")   # → "AAPL"
        ticker = mapper.resolve("Lockheed Martin")  # → "LMT"
    """

    RATE_LIMIT_DELAY: float = 0.5   # seconds between yfinance calls
    MAX_SEARCH_RESULTS: int = 5     # candidates to evaluate per search

    def __init__(self) -> None:
        self._overrides: dict[str, str] = self._load_overrides()
        # In-process cache: normalised_name → ticker (or None = confirmed miss)
        self._cache: dict[str, str | None] = {}
        logger.debug(
            "[TickerMapper] Loaded {} manual overrides.", len(self._overrides)
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def resolve(self, company_name: str) -> str | None:
        """
        Resolve a company name to a ticker symbol.

        Returns the ticker string (e.g. "AAPL") or None if no match
        could be found through any resolution path.
        """
        if not company_name or not company_name.strip():
            return None

        key = self._normalise(company_name)

        # 1 — in-process cache (includes confirmed misses stored as None)
        if key in self._cache:
            return self._cache[key]

        # 2 — manual overrides (case-insensitive)
        ticker = self._check_overrides(key)
        if ticker:
            logger.debug("[TickerMapper] Override hit: {!r} → {}", company_name, ticker)
            self._cache[key] = ticker
            self._ensure_company(ticker, company_name, manually_overridden=True)
            return ticker

        # 3 — database cache
        ticker = self._check_db_cache(key)
        if ticker:
            logger.debug("[TickerMapper] DB cache hit: {!r} → {}", company_name, ticker)
            self._cache[key] = ticker
            return ticker

        # 4 — live yfinance lookup
        ticker = self._search_yfinance(company_name)
        if ticker:
            logger.info("[TickerMapper] Resolved {!r} → {}", company_name, ticker)
            self._cache[key] = ticker
            self._upsert_company(ticker, company_name)
            return ticker

        # 5 — heuristic: strip legal suffixes and retry
        stripped = self._strip_suffixes(company_name)
        if stripped != key:
            ticker = self._search_yfinance(stripped)
            if ticker:
                logger.info(
                    "[TickerMapper] Resolved (stripped) {!r} → {!r} → {}",
                    company_name,
                    stripped,
                    ticker,
                )
                self._cache[key] = ticker
                self._upsert_company(ticker, company_name)
                return ticker

        # Confirmed miss
        logger.warning(
            "[TickerMapper] No ticker found for {!r}. "
            "Add to data/ticker_overrides.json to resolve manually.",
            company_name,
        )
        self._cache[key] = None
        return None

    def resolve_batch(self, company_names: list[str]) -> dict[str, str | None]:
        """
        Resolve a list of company names.

        Returns a dict mapping each input name to its ticker (or None).
        Deduplicates internally so each unique name is only looked up once.
        """
        unique = list(dict.fromkeys(company_names))  # preserve order, dedupe
        results: dict[str, str | None] = {}

        for name in unique:
            results[name] = self.resolve(name)
            time.sleep(self.RATE_LIMIT_DELAY)

        return {name: results[name] for name in company_names}

    def enrich_unresolved_trades(self) -> int:
        """
        Find all Trade rows where ticker IS NULL and attempt resolution.

        Called by the daily scheduler after the collector runs.
        Returns count of trades that were successfully resolved.
        """
        from models import Trade  # local import to avoid circular deps

        resolved_count = 0
        with get_session() as session:
            stmt = select(Trade).where(Trade.ticker.is_(None))
            trades = session.scalars(stmt).all()

            logger.info(
                "[TickerMapper] {} trades have no ticker — attempting resolution.",
                len(trades),
            )

            for trade in trades:
                ticker = self.resolve(trade.company_name)
                if ticker:
                    trade.ticker = ticker
                    resolved_count += 1
                    logger.debug(
                        "[TickerMapper] Assigned ticker {} to trade id={}.",
                        ticker,
                        trade.id,
                    )
                time.sleep(self.RATE_LIMIT_DELAY)

        logger.info(
            "[TickerMapper] Resolved {}/{} previously unmatched trades.",
            resolved_count,
            len(trades) if trades else 0,
        )
        return resolved_count

    def reload_overrides(self) -> None:
        """Reload the overrides file and clear the in-process cache."""
        self._overrides = self._load_overrides()
        self._cache.clear()
        logger.info(
            "[TickerMapper] Reloaded {} overrides, cache cleared.",
            len(self._overrides),
        )

    # ------------------------------------------------------------------ #
    # Resolution steps
    # ------------------------------------------------------------------ #

    def _check_overrides(self, normalised_name: str) -> str | None:
        """Check the manual overrides dict (normalised keys)."""
        return self._overrides.get(normalised_name)

    def _check_db_cache(self, normalised_name: str) -> str | None:
        """
        Look for a Company row whose company_name normalises to the key.

        We check both exact match and the stored normalised name.
        """
        with get_session() as session:
            # Exact company_name match (case-insensitive handled at Python level)
            stmt = select(Company)
            companies = session.scalars(stmt).all()
            for company in companies:
                if self._normalise(company.company_name) == normalised_name:
                    if company.ticker_verified or company.manually_overridden:
                        return company.ticker
        return None

    def _search_yfinance(self, query: str) -> str | None:
        """
        Search yfinance for a ticker matching the query string.

        yfinance.Search returns candidates ranked by relevance.
        We take the top result if its name closely matches our query.
        """
        try:
            time.sleep(self.RATE_LIMIT_DELAY)
            search = yf.Search(query, max_results=self.MAX_SEARCH_RESULTS)
            quotes = search.quotes

            if not quotes:
                return None

            query_norm = self._normalise(query)

            for candidate in quotes:
                # Only consider equity types
                type_disp = (candidate.get("typeDisp") or "").lower()
                if type_disp not in ("equity", "etf", ""):
                    continue

                symbol: str = (candidate.get("symbol") or "").upper().strip()
                long_name: str = candidate.get("longname") or candidate.get("shortname") or ""

                if not symbol:
                    continue

                # Score similarity between query and candidate name
                cand_norm = self._normalise(long_name)
                if self._names_match(query_norm, cand_norm, symbol):
                    return symbol

            # Fallback: just take the first equity result
            for candidate in quotes:
                if (candidate.get("typeDisp") or "").lower() in ("equity", ""):
                    symbol = (candidate.get("symbol") or "").upper().strip()
                    if symbol:
                        logger.debug(
                            "[TickerMapper] Accepting first-result fallback: {} for {!r}",
                            symbol,
                            query,
                        )
                        return symbol

        except Exception as exc:
            logger.warning(
                "[TickerMapper] yfinance search failed for {!r}: {}", query, exc
            )

        return None

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #

    def _ensure_company(
        self,
        ticker: str,
        company_name: str,
        manually_overridden: bool = False,
    ) -> None:
        """Insert a Company row if one doesn't already exist for this ticker."""
        with get_session() as session:
            existing = session.get(Company, ticker)
            if existing is None:
                company = Company(
                    ticker=ticker,
                    company_name=company_name,
                    ticker_verified=True,
                    manually_overridden=manually_overridden,
                )
                session.add(company)
                logger.debug("[TickerMapper] Created Company row for {}.", ticker)

    def _upsert_company(self, ticker: str, company_name: str) -> None:
        """
        Insert or update a Company row and enrich with yfinance metadata.
        """
        info = self._fetch_yfinance_info(ticker)

        with get_session() as session:
            existing = session.get(Company, ticker)
            if existing is None:
                company = Company(
                    ticker=ticker,
                    company_name=info.get("longName") or company_name,
                    sector=info.get("sector"),
                    industry=info.get("industry"),
                    market_cap=info.get("marketCap"),
                    ticker_verified=True,
                    manually_overridden=False,
                    last_updated=_now(),
                )
                session.add(company)
                logger.debug(
                    "[TickerMapper] Inserted Company: {} / {} / {}",
                    ticker,
                    company.company_name,
                    company.sector,
                )
            else:
                # Only overwrite fields if not manually set
                if not existing.manually_overridden:
                    existing.sector = info.get("sector") or existing.sector
                    existing.industry = info.get("industry") or existing.industry
                    existing.market_cap = info.get("marketCap") or existing.market_cap
                existing.ticker_verified = True
                existing.last_updated = _now()
                logger.debug("[TickerMapper] Updated Company: {}.", ticker)

    def _fetch_yfinance_info(self, ticker: str) -> dict:
        """Fetch basic info dict from yfinance; returns {} on failure."""
        try:
            time.sleep(self.RATE_LIMIT_DELAY)
            info = yf.Ticker(ticker).info
            return info or {}
        except Exception as exc:
            logger.warning(
                "[TickerMapper] yfinance info failed for {}: {}", ticker, exc
            )
            return {}

    # ------------------------------------------------------------------ #
    # Override file management
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_overrides() -> dict[str, str]:
        """
        Load data/ticker_overrides.json.

        File format::

            {
                "apple inc": "AAPL",
                "alphabet": "GOOGL",
                "meta platforms": "META"
            }

        Keys are normalised (lowercase, stripped).
        Returns empty dict if file is missing or malformed.
        """
        path: Path = settings.ticker_overrides_path
        if not path.exists():
            logger.debug("[TickerMapper] No overrides file found at {}.", path)
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Normalise keys
            return {
                k.lower().strip(): v.upper().strip()
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("[TickerMapper] Failed to load overrides file: {}", exc)
            return {}

    @staticmethod
    def save_override(company_name: str, ticker: str) -> None:
        """
        Append a manual mapping to ticker_overrides.json.

        Thread-safe at the file level (read-modify-write within one call).
        """
        path: Path = settings.ticker_overrides_path
        overrides: dict = {}
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    overrides = json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass

        overrides[company_name.lower().strip()] = ticker.upper().strip()

        with path.open("w", encoding="utf-8") as fh:
            json.dump(overrides, fh, indent=2)

        logger.info(
            "[TickerMapper] Saved override: {!r} → {}", company_name, ticker
        )

    # ------------------------------------------------------------------ #
    # String utilities
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalise(name: str) -> str:
        """Lowercase, collapse whitespace, strip punctuation for comparison."""
        name = name.lower().strip()
        name = re.sub(r"[^\w\s]", " ", name)
        name = _WHITESPACE.sub(" ", name).strip()
        return name

    @staticmethod
    def _strip_suffixes(name: str) -> str:
        """Remove common legal suffixes and clean up residual whitespace."""
        stripped = _LEGAL_SUFFIXES.sub(" ", name)
        stripped = _WHITESPACE.sub(" ", stripped).strip(" ,.")
        return stripped

    @staticmethod
    def _names_match(query_norm: str, candidate_norm: str, symbol: str) -> bool:
        """
        Return True if the candidate is a plausible match for the query.

        Uses three heuristics:
          • Exact normalised match
          • Query is a substring of candidate (or vice versa)
          • Significant word overlap (Jaccard ≥ 0.5)
        """
        if not query_norm or not candidate_norm:
            return False

        if query_norm == candidate_norm:
            return True

        if query_norm in candidate_norm or candidate_norm in query_norm:
            return True

        query_words = set(query_norm.split())
        cand_words = set(candidate_norm.split())

        # Ignore very short stop-like tokens
        query_words -= {"the", "a", "an", "of", "and", "&"}
        cand_words -= {"the", "a", "an", "of", "and", "&"}

        if not query_words or not cand_words:
            return False

        intersection = query_words & cand_words
        union = query_words | cand_words
        jaccard = len(intersection) / len(union)

        return jaccard >= 0.5


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _now() -> "datetime":
    from datetime import datetime
    return datetime.utcnow()


# Singleton instance for use across the application
_mapper_instance: TickerMapper | None = None


def get_ticker_mapper() -> TickerMapper:
    """Return the shared TickerMapper singleton (lazy initialised)."""
    global _mapper_instance
    if _mapper_instance is None:
        _mapper_instance = TickerMapper()
    return _mapper_instance
