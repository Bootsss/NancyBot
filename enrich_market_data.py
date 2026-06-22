"""
Run this once to populate company names, sectors, and market data.
    python enrich_market_data.py

Takes 5-10 minutes as it calls yfinance for each ticker.
"""
import os
os.environ.setdefault("DISCORD_TOKEN", "placeholder")
os.environ.setdefault("DISCORD_CHANNEL_ID", "123456789")

from dotenv import load_dotenv
load_dotenv()

from config import configure_logging
configure_logging()

from database import init_db, get_session
from models import Company
from sqlalchemy import select
import yfinance as yf
import time

init_db()

with get_session() as session:
    companies = session.scalars(select(Company)).all()
    tickers = [c.ticker for c in companies]

print(f"Enriching {len(tickers)} tickers with yfinance data...")

for i, ticker in enumerate(tickers, 1):
    try:
        info = yf.Ticker(ticker).info
        if not info or len(info) < 5:
            print(f"  [{i}/{len(tickers)}] {ticker}: no data")
            continue

        with get_session() as session:
            company = session.get(Company, ticker)
            if company:
                company.company_name = info.get("longName") or info.get("shortName") or company.company_name
                company.sector = info.get("sector") or company.sector
                company.industry = info.get("industry") or company.industry
                company.market_cap = info.get("marketCap")
                company.current_price = info.get("currentPrice") or info.get("regularMarketPrice")
                company.pe_ratio = info.get("trailingPE")
                company.avg_volume = info.get("averageVolume")
                company.ticker_verified = True
                from datetime import datetime
                company.last_updated = datetime.utcnow()

        print(f"  [{i}/{len(tickers)}] {ticker}: {info.get('longName', 'N/A')} | {info.get('sector', 'N/A')}")
        time.sleep(0.5)

    except Exception as e:
        print(f"  [{i}/{len(tickers)}] {ticker}: ERROR — {e}")
        time.sleep(1)

print("\nDone! Restart the bot and try !top again.")
