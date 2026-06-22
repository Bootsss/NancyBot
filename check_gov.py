"""
python check_gov.py
Tests official government congressional disclosure sources.
"""
import requests, json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*",
}

sources = [
    ("Senate eFD search",
     "https://efts.senate.gov/LATEST/search-index?q=%22purchase%22&dateRange=custom&fromDate=2026-01-01&toDate=2026-06-22&results=10"),
    ("Senate eFD direct",
     "https://efts.senate.gov/LATEST/search-index?q=&results=10&sort=date_filed"),
    ("House clerk search",
     "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult?Name=&StateDst=&FilerType=P&FilingYear=2026&DocType=P&SubmitButton=Search"),
    ("House clerk API",
     "https://disclosures-clerk.house.gov/api/FinancialDisclosure?pageSize=10&filYear=2026"),
    ("Capitol Trades (browser)",
     "https://www.capitoltrades.com/trades?pageSize=10"),
    ("Congress.gov trades",
     "https://api.congress.gov/v3/congressional-record?limit=5&api_key=DEMO_KEY"),
]

for name, url in sources:
    try:
        r = requests.get(url, timeout=15, headers=headers)
        print(f"\n{name}: HTTP {r.status_code}")
        if r.status_code == 200:
            ct = r.headers.get("content-type","")
            if "json" in ct:
                data = r.json()
                print(f"  ✅ JSON — type={type(data).__name__}")
                if isinstance(data, list):
                    print(f"  {len(data)} records")
                    if data: print(f"  Keys: {list(data[0].keys())}")
                elif isinstance(data, dict):
                    print(f"  Keys: {list(data.keys())[:6]}")
                print(f"  Sample: {json.dumps(data)[:300]}")
            else:
                print(f"  Content-Type: {ct}")
                print(f"  First 200 chars: {r.text[:200]}")
        else:
            print(f"  ❌ {r.text[:100]}")
    except Exception as e:
        print(f"  ERROR: {e}")

print("\nDone.")
