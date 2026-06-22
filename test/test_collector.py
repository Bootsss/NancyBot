"""
tests/test_collector.py — Trade collection and parsing tests.

Covers:
  - BaseCollector amount range parsing
  - BaseCollector date parsing
  - CongressCollector raw row → Trade mapping
  - Duplicate prevention via unique constraint
  - Graceful handling of missing fields
  - run_all_collectors registry
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from services.trade_collector import (
    BaseCollector,
    CongressCollector,
    CollectorError,
    run_all_collectors,
)
from tests.conftest import make_trade


# ---------------------------------------------------------------------------
# Amount range parsing
# ---------------------------------------------------------------------------

class TestAmountRangeParsing:

    def test_exact_match(self):
        lo, hi = BaseCollector.parse_amount_range("$50,001 - $100,000")
        assert lo == 50_001
        assert hi == 100_000

    def test_large_range(self):
        lo, hi = BaseCollector.parse_amount_range("$1,000,001 - $5,000,000")
        assert lo == 1_000_001
        assert hi == 5_000_000

    def test_over_5m_range(self):
        lo, hi = BaseCollector.parse_amount_range("Over $5,000,000")
        assert lo == 5_000_001

    def test_none_input(self):
        lo, hi = BaseCollector.parse_amount_range(None)
        assert lo is None
        assert hi is None

    def test_empty_string(self):
        lo, hi = BaseCollector.parse_amount_range("")
        assert lo is None
        assert hi is None

    def test_unrecognised_range_returns_none(self):
        lo, hi = BaseCollector.parse_amount_range("$999 - $1,000 (custom)")
        assert lo is None
        assert hi is None

    def test_all_standard_ranges_parse(self):
        from config import AMOUNT_RANGE_MAP
        for range_str in AMOUNT_RANGE_MAP:
            lo, hi = BaseCollector.parse_amount_range(range_str)
            assert lo is not None, f"Failed to parse: {range_str}"
            assert hi is not None, f"Failed to parse hi: {range_str}"
            assert lo < hi


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

class TestDateParsing:

    def test_iso_format(self):
        dt = BaseCollector.parse_date("2024-03-15")
        assert dt == datetime(2024, 3, 15)

    def test_us_format(self):
        dt = BaseCollector.parse_date("03/15/2024")
        assert dt == datetime(2024, 3, 15)

    def test_iso_datetime(self):
        dt = BaseCollector.parse_date("2024-03-15T14:30:00")
        assert dt == datetime(2024, 3, 15, 14, 30, 0)

    def test_none_input(self):
        assert BaseCollector.parse_date(None) is None

    def test_empty_string(self):
        assert BaseCollector.parse_date("") is None

    def test_unparseable_string(self):
        assert BaseCollector.parse_date("not-a-date") is None

    def test_custom_format(self):
        dt = BaseCollector.parse_date("15/03/2024", formats=["%d/%m/%Y"])
        assert dt == datetime(2024, 3, 15)


# ---------------------------------------------------------------------------
# CongressCollector parsing
# ---------------------------------------------------------------------------

class TestCongressCollectorParsing:

    def _make_row(self, **overrides) -> dict:
        base = {
            "Name": "Nancy Pelosi",
            "Ticker": "AAPL",
            "Transaction": "Purchase",
            "TransactionDate": "2024-03-15",
            "ReportDate": "2024-03-18",
            "Range": "$50,001 - $100,000",
            "House": "House",
            "Party": "Democrat",
            "Company": "Apple Inc.",
        }
        base.update(overrides)
        return base

    def test_parse_buy_row(self):
        collector = CongressCollector()
        row = self._make_row()
        trade = collector._parse_single(row)
        assert trade is not None
        assert trade.ticker == "AAPL"
        assert trade.politician_name == "Nancy Pelosi"
        assert trade.trade_type == "purchase"
        assert trade.amount_min == 50_001
        assert trade.amount_max == 100_000

    def test_parse_sale_row(self):
        collector = CongressCollector()
        row = self._make_row(Transaction="Sale")
        trade = collector._parse_single(row)
        assert trade is not None
        assert trade.trade_type == "sale"

    def test_parse_sale_full_normalised(self):
        collector = CongressCollector()
        row = self._make_row(Transaction="Sale (Full)")
        trade = collector._parse_single(row)
        assert trade.trade_type == "sale"

    def test_parse_sale_partial_normalised(self):
        collector = CongressCollector()
        row = self._make_row(Transaction="Sale (Partial)")
        trade = collector._parse_single(row)
        assert trade.trade_type == "sale"

    def test_missing_politician_returns_none(self):
        collector = CongressCollector()
        row = self._make_row(Name="")
        assert collector._parse_single(row) is None

    def test_missing_trade_date_returns_none(self):
        collector = CongressCollector()
        row = self._make_row(TransactionDate="")
        assert collector._parse_single(row) is None

    def test_unknown_transaction_type_returns_none(self):
        collector = CongressCollector()
        row = self._make_row(Transaction="")
        assert collector._parse_single(row) is None

    def test_missing_ticker_stored_as_none(self):
        collector = CongressCollector()
        row = self._make_row(Ticker="")
        trade = collector._parse_single(row)
        assert trade is not None
        assert trade.ticker is None

    def test_ticker_uppercased(self):
        collector = CongressCollector()
        row = self._make_row(Ticker="aapl")
        trade = collector._parse_single(row)
        assert trade.ticker == "AAPL"

    def test_raw_data_stored_as_json(self):
        import json
        collector = CongressCollector()
        row = self._make_row()
        trade = collector._parse_single(row)
        assert trade.raw_data is not None
        parsed = json.loads(trade.raw_data)
        assert parsed["Name"] == "Nancy Pelosi"

    def test_parse_records_skips_bad_rows(self):
        collector = CongressCollector()
        rows = [
            self._make_row(),           # good
            self._make_row(Name=""),    # bad — missing politician
            self._make_row(),           # good
        ]
        trades = collector._parse_records(rows)
        assert len(trades) == 2

    def test_parse_records_empty_input(self):
        collector = CongressCollector()
        trades = collector._parse_records([])
        assert trades == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:

    def test_persist_skips_duplicate(self, db_engine):
        """
        Use real committed sessions so the unique constraint is visible
        on the second _persist call.  Pin trade_date so both objects
        share an identical (politician, ticker, date, type) identity.
        """
        from datetime import datetime as _dt
        from sqlalchemy.orm import sessionmaker as _sm
        from unittest.mock import patch
        from contextlib import contextmanager

        Session = _sm(bind=db_engine, autocommit=False, autoflush=False)
        fixed_date = _dt(2024, 6, 1, 12, 0, 0)

        @contextmanager
        def real_session_cm():
            s = Session()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        collector = CongressCollector()

        with patch("services.trade_collector.get_session", real_session_cm):
            count1 = collector._persist([make_trade(trade_date=fixed_date)])
            count2 = collector._persist([make_trade(trade_date=fixed_date)])

        assert count1 == 1
        assert count2 == 0

    def test_persist_allows_different_politicians(self, db_session):
        with patch("services.trade_collector.get_session") as mock_gs:
            mock_gs.return_value.__enter__ = lambda s: db_session
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            collector = CongressCollector()
            t1 = make_trade(politician_name="Pelosi")
            t2 = make_trade(politician_name="Ryan")
            count = collector._persist([t1, t2])

        assert count == 2


# ---------------------------------------------------------------------------
# run_all_collectors
# ---------------------------------------------------------------------------

class TestRunAllCollectors:

    def test_run_all_collectors_returns_dict(self):
        with patch.object(CongressCollector, "collect", return_value=5):
            results = run_all_collectors()
        assert isinstance(results, dict)
        assert "congress" in results
        assert results["congress"] == 5

    def test_run_all_collectors_handles_failure(self):
        """A failing collector returns 0 and does not raise."""
        with patch.object(CongressCollector, "collect", side_effect=Exception("network error")):
            results = run_all_collectors()
        assert results["congress"] == 0

    def test_run_all_collectors_handles_collector_error(self):
        with patch.object(CongressCollector, "collect", side_effect=CollectorError("api down")):
            results = run_all_collectors()
        assert results["congress"] == 0
