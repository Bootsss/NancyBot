"""
python check_fmp.py
Tests which FMP congressional endpoints your key can access.
"""
import requests, json

KEY = "NztxBgaBp71yB7vIbZlCOh60bS04vtJu"

endpoints = [
    ("Senate Trading",       f"https://financialmodelingprep.com/api/v4/senate-trading?symbol=AAPL&apikey={KEY}"),
    ("Senate RSS Feed",      f"https://financialmodelingprep.com/api/v4/senate-trading-rss-feed?apikey={KEY}&page=0"),
    ("House Disclosure",     f"https://financialmodelingprep.com/api/v4/house-disclosure?symbol=AAPL&apikey={KEY}"),
    ("House RSS Feed",       f"https://financialmodelingprep.com/api/v4/house-disclosure-rss-feed?apikey={KEY}&page=0"),
    ("Stable Senate",        f"https://financialmodelingprep.com/stable/senate-trading?apikey={KEY}"),
    ("Stable House",         f"https://financialmodelingprep.com/stable/house-disclosure?apikey={KEY}"),
]

for name, url in endpoints:
    try:
        r = requests.get(url, timeout=15)
        print(f"\n{name}: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                print(f"  ✅ SUCCESS — {len(data)} records")
                if data:
                    print(f"  Keys: {list(data[0].keys())}")
                    print(f"  Sample:\n{json.dumps(data[0], indent=2)[:400]}")
            else:
                print(f"  Response: {json.dumps(data)[:200]}")
        else:
            print(f"  ❌ {r.text[:150]}")
    except Exception as e:
        print(f"  ERROR: {e}")

print("\nDone.")
