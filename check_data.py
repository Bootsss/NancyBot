"""
Run this to see what format the free Quiver data is actually in.
    python check_data.py
"""
import os, json
os.environ['DISCORD_TOKEN'] = 'test'
os.environ['DISCORD_CHANNEL_ID'] = '123456789'

import requests

url = "https://api.quiverquant.com/beta/historical/congresstrading/AAPL"
headers = {
    "User-Agent": "CapitolGains-Bot/1.0",
    "Accept": "application/json",
}

print(f"Fetching {url}...")
r = requests.get(url, timeout=30, headers=headers)
print(f"Status: {r.status_code}")

if r.status_code == 200:
    data = r.json()
    print(f"Type: {type(data)}")
    if isinstance(data, list):
        print(f"Records: {len(data)}")
        if data:
            print(f"\nFirst record keys: {list(data[0].keys())}")
            print(f"\nFirst record:\n{json.dumps(data[0], indent=2)}")
            print(f"\nSecond record:\n{json.dumps(data[1], indent=2)}")
    elif isinstance(data, dict):
        print(f"Top-level keys: {list(data.keys())}")
else:
    print(f"Error: {r.text[:200]}")
