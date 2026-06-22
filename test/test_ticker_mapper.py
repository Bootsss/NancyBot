"""
tests/test_ticker_mapper.py — Ticker mapper resolution tests.

Covers:
  - Name normalisation
  - Legal suffix stripping
  - Name similarity matching heuristics
  - Manual override loading and lookup
  - save_override writes to file
  - Cache hit on second call
  - Confirmed miss cached as None
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.ticker_mapper import TickerMapper


# ---------------------------------------------------------------------------
# String utilities
# ---------------------------------------------------------------------------

class TestNormalise:

    def test_lowercase_and_strip(self):
        assert TickerMapper._normalise("  Apple Inc.  ") == "apple inc"

    def test_punctuation_replaced(self):
        assert TickerMapper._normalise("AT&T Inc.") == "at t inc"

    def test_double_spaces_collapsed(self):
        result = TickerMapper._normalise("Apple  Inc")
        assert "  " not in result

    def test_empty_string(self):
        assert TickerMapper._normalise("") == ""


class TestStripSuffixes:

    def test_strips_inc(self):
        result = TickerMapper._strip_suffixes("Apple Inc.")
        assert "Inc" not in result
        assert "Apple" in result

    def test_strips_corporation(self):
        result = TickerMapper._strip_suffixes("Lockheed Martin Corporation")
        assert "Corporation" not in result

    def test_strips_llc(self):
        result = TickerMapper._strip_suffixes("Acme LLC")
        assert "LLC" not in result

    def test_strips_holdings(self):
        result = TickerMapper._strip_suffixes("Berkshire Hathaway Holdings")
        assert "Holdings" not in result

    def test_preserves_core_name(self):
        result = TickerMapper._strip_suffixes("Microsoft Corporation")
        assert "Microsoft" in result

    def test_no_suffix_unchanged(self):
        result = TickerMapper._strip_suffixes("Apple")
        assert result.strip() == "Apple"


class TestNamesMatch:

    def test_exact_match(self):
        assert TickerMapper._names_match("apple", "apple", "AAPL") is True

    def test_substring_match(self):
        assert TickerMapper._names_match("apple", "apple inc", "AAPL") is True

    def test_reverse_substring(self):
        assert TickerMapper._names_match("apple inc", "apple", "AAPL") is True

    def test_high_jaccard(self):
        # "nvidia corp" vs "nvidia corporation" — high word overlap
        assert TickerMapper._names_match("nvidia corp", "nvidia corporation", "NVDA") is True

    def test_low_jaccard_no_match(self):
        assert TickerMapper._names_match("apple", "microsoft", "MSFT") is False

    def test_empty_query_no_match(self):
        assert TickerMapper._names_match("", "apple", "AAPL") is False


# ---------------------------------------------------------------------------
# Override loading
# ---------------------------------------------------------------------------

class TestOverrideLoading:

    def test_load_overrides_from_valid_file(self, tmp_path):
        override_file = tmp_path / "overrides.json"
        override_file.write_text(json.dumps({"Apple Inc": "AAPL", "Alphabet": "GOOGL"}))

        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = override_file
            overrides = TickerMapper._load_overrides()

        assert overrides.get("apple inc") == "AAPL"
        assert overrides.get("alphabet") == "GOOGL"

    def test_load_overrides_missing_file(self, tmp_path):
        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = tmp_path / "nonexistent.json"
            overrides = TickerMapper._load_overrides()

        assert overrides == {}

    def test_load_overrides_invalid_json(self, tmp_path):
        override_file = tmp_path / "bad.json"
        override_file.write_text("{ not valid json }")

        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = override_file
            overrides = TickerMapper._load_overrides()

        assert overrides == {}

    def test_keys_are_normalised_lowercase(self, tmp_path):
        override_file = tmp_path / "overrides.json"
        override_file.write_text(json.dumps({"APPLE INC": "AAPL"}))

        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = override_file
            overrides = TickerMapper._load_overrides()

        assert "apple inc" in overrides

    def test_values_are_uppercased(self, tmp_path):
        override_file = tmp_path / "overrides.json"
        override_file.write_text(json.dumps({"apple": "aapl"}))

        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = override_file
            overrides = TickerMapper._load_overrides()

        assert overrides["apple"] == "AAPL"

    def test_save_override_writes_file(self, tmp_path):
        override_file = tmp_path / "overrides.json"
        override_file.write_text("{}")

        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = override_file
            TickerMapper.save_override("Alphabet Inc", "GOOGL")

        saved = json.loads(override_file.read_text())
        assert saved.get("alphabet inc") == "GOOGL"

    def test_save_override_appends_to_existing(self, tmp_path):
        override_file = tmp_path / "overrides.json"
        override_file.write_text(json.dumps({"apple inc": "AAPL"}))

        with patch("services.ticker_mapper.settings") as mock_settings:
            mock_settings.ticker_overrides_path = override_file
            TickerMapper.save_override("Alphabet", "GOOGL")

        saved = json.loads(override_file.read_text())
        assert "apple inc" in saved
        assert "alphabet" in saved


# ---------------------------------------------------------------------------
# Resolution flow
# ---------------------------------------------------------------------------

class TestResolution:

    def _mapper_with_overrides(self, overrides: dict) -> TickerMapper:
        """Create a TickerMapper with the given overrides pre-loaded."""
        mapper = TickerMapper.__new__(TickerMapper)
        mapper._overrides = {k.lower(): v.upper() for k, v in overrides.items()}
        mapper._cache = {}
        return mapper

    def test_override_takes_priority(self):
        mapper = self._mapper_with_overrides({"apple inc": "AAPL"})

        with patch.object(mapper, "_check_db_cache", return_value=None), \
             patch.object(mapper, "_search_yfinance", return_value=None), \
             patch.object(mapper, "_ensure_company"):

            result = mapper.resolve("Apple Inc.")

        assert result == "AAPL"

    def test_cache_hit_returns_without_db(self):
        mapper = self._mapper_with_overrides({})
        mapper._cache["apple inc"] = "AAPL"

        with patch.object(mapper, "_check_db_cache") as mock_db:
            result = mapper.resolve("Apple Inc.")

        assert result == "AAPL"
        mock_db.assert_not_called()

    def test_cache_stores_confirmed_miss(self):
        mapper = self._mapper_with_overrides({})

        with patch.object(mapper, "_check_db_cache", return_value=None), \
             patch.object(mapper, "_search_yfinance", return_value=None), \
             patch.object(mapper, "_strip_suffixes", return_value="apple inc"):

            result = mapper.resolve("Apple Inc.")

        assert result is None
        assert mapper._cache.get("apple inc") is None   # stored as None

    def test_second_call_uses_cache(self):
        mapper = self._mapper_with_overrides({})

        with patch.object(mapper, "_check_db_cache", return_value=None), \
             patch.object(mapper, "_search_yfinance", return_value="AAPL") as mock_yf, \
             patch.object(mapper, "_upsert_company"):

            mapper.resolve("Apple Inc.")
            mapper.resolve("Apple Inc.")   # second call

        # yfinance only called once
        assert mock_yf.call_count == 1

    def test_none_input_returns_none(self):
        mapper = self._mapper_with_overrides({})
        assert mapper.resolve(None) is None

    def test_empty_string_returns_none(self):
        mapper = self._mapper_with_overrides({})
        assert mapper.resolve("") is None

    def test_resolve_batch_deduplicates(self):
        mapper = self._mapper_with_overrides({"apple inc": "AAPL"})

        with patch.object(mapper, "_check_db_cache", return_value=None), \
             patch.object(mapper, "_ensure_company"):

            results = mapper.resolve_batch(["Apple Inc.", "Apple Inc.", "Apple Inc."])

        assert results["Apple Inc."] == "AAPL"

    def test_stripped_suffix_fallback(self):
        """If the original name fails, the stripped version should be tried."""
        mapper = self._mapper_with_overrides({})

        call_count = {"n": 0}

        def mock_search(query: str):
            call_count["n"] += 1
            if "Inc" not in query:
                return "AAPL"
            return None

        with patch.object(mapper, "_check_db_cache", return_value=None), \
             patch.object(mapper, "_search_yfinance", side_effect=mock_search), \
             patch.object(mapper, "_upsert_company"):

            result = mapper.resolve("Apple Inc.")

        assert result == "AAPL"
        assert call_count["n"] == 2   # tried original + stripped
