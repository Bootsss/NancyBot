"""
python check_quiver.py
Shows the actual field names Quiver is returning.
"""
import requests, json

KEY = "00bbf760c749fa87d46527c2f9470ff7dfff3209"
headers = {
    "Accept": "application/json",
    "Authorization": f"Token {KEY}",
    "X-CSRFToken": KEY,
}

r = requests.get(
    "https://api.quiverquant.com/beta/live/congresstrading",
    headers=headers,
    params={"page": 1, "page_size": 3},
    timeout=30,
)

print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    print(f"Type: {type(data)}, Length: {len(data) if isinstance(data, list) else 'N/A'}")
    if isinstance(data, list) and data:
        print(f"\nFirst record keys: {list(data[0].keys())}")
        print(f"\nFirst record:\n{json.dumps(data[0], indent=2)}")
        if len(data) > 1:
            print(f"\nSecond record:\n{json.dumps(data[1], indent=2)}")
else:
    print(f"Error: {r.text[:300]}")
