"""
Run this once to seed the database with full trade history.
    python seed_data.py
"""
from dotenv import load_dotenv
load_dotenv()

from config import configure_logging
configure_logging()

from database import init_db
init_db()

print("Step 1/4: Collecting trades from Quiver (this takes 2-3 mins)...")
from services.trade_collector import run_all_collectors
results = run_all_collectors()
print(f"  Done — {sum(results.values())} trades inserted")

print("Step 2/4: Seeding company rows...")
from scheduler import job_seed_companies
job_seed_companies()
print("  Done")

print("Step 3/4: Scoring all tickers...")
from services.scoring_engine import get_scoring_engine
scores = get_scoring_engine().score_all()
print(f"  Done — {len(scores)} tickers scored")

print("Step 4/4: Generating alerts...")
from services.alert_service import AlertService
alerts = AlertService().generate_alerts()
print(f"  Done — {len(alerts)} alerts generated")

print("\nAll done! Now run: python bot.py")
