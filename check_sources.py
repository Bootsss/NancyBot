"""
Run this to find which free data sources work on your machine.
    python check_sources.py
"""
import requests
import json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

sources = [
    ("House Stock Watcher (new)",   "https://house-stock-watcher-data.s3.amazonaws.com/data/all_transactions.json"),
    ("Senate Stock Watcher (new)",  "https://senate-stock-watcher-data.s3.amazonaws.com/aggregate/all_transactions.json"),
    ("CapitolTrades House",         "https://api.capitoltrades.com/trades?chamber=house&pageSize=10"),
    ("CapitolTrades Senate",        "https://api.capitoltrades.com/trades?chamber=senate&pageSize=10"),
    ("FMP Senate (free key)",       "https://financialmodelingprep.com/api/v4/senate-trading?symbol=AAPL&apikey=demo"),
    ("Congress.gov",                "https://api.congress.gov/v3/member?limit=5&api_key=DEMO_KEY"),
    ("Quiver Public Congress",      "https://api.quiverquant.com/beta/live/congresstrading"),
]

for name, url in sources:
    print(f"\nTesting {name}...")
    try:
        r = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
        print(f"  Status: {r.status_code}")
        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, list):
                    print(f"  SUCCESS - {len(data)} records")
                    if data:
                        print(f"  Keys: {list(data[0].keys())}")
                        print(f"  Sample:\n{json.dumps(data[0], indent=2)[:500]}")
                elif isinstance(data, dict):
                    print(f"  SUCCESS - dict keys: {list(data.keys())[:8]}")
                    print(f"  Sample:\n{json.dumps(data, indent=2)[:300]}")
            except Exception:
                print(f"  Not JSON: {r.text[:150]}")
        else:
            print(f"  FAILED: {r.text[:120]}")
    except Exception as e:
        print(f"  ERROR: {e}")

print("\nDone.")
