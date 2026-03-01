import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
import json
import os
import argparse

# --- CONFIGURATION ---
DEFAULT_API_KEY = "sk-YOUR_TOKEN_HERE"
BASE_URL = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/"

def get_pstryk_data(api_key, hours_ahead=48):
    """
    Fetches pricing data from Pstryk API for the specified window using standard urllib.
    """
    now = datetime.now(timezone.utc)
    # Start from the current hour (snapped) instead of next hour
    window_start = now.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    window_end = (now + timedelta(hours=hours_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')

    params = {
        "metrics": "pricing",
        "resolution": "hour",
        "window_start": window_start,
        "window_end": window_end
    }
    
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": api_key,
        "Accept": "application/json"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                return json.loads(response.read().decode('utf-8'))
            else:
                return {"error": f"HTTP Error {response.status}"}
    except Exception as e:
        return {"error": str(e)}

def find_cheapest_window(data):
    if not data or "error" in data:
        return data

    frames = data.get("frames", [])
    if not frames:
        return {"error": "No frames in data"}

    valid_frames = [
        f for f in frames 
        if "pricing" in f.get("metrics", {}) and f["metrics"]["pricing"].get("price_net") is not None
    ]
    
    if not valid_frames:
        return {"error": "No valid pricing data in frames"}

    # 1. Find the absolute minimum GROSS price and its index
    min_f = min(valid_frames, key=lambda x: x["metrics"]["pricing"]["price_gross"])
    min_price = min_f["metrics"]["pricing"]["price_gross"]
    min_idx = valid_frames.index(min_f)
    
    start_idx = min_idx
    end_idx = min_idx
    
    # 2. Expand forward: include next hour if it's cheaper OR rises by < 10%
    while end_idx + 1 < len(valid_frames):
        curr_p = valid_frames[end_idx]["metrics"]["pricing"]["price_gross"]
        next_p = valid_frames[end_idx + 1]["metrics"]["pricing"]["price_gross"]
        
        # 10% increase limit, with small epsilon for low prices
        limit = max(curr_p * 1.10, curr_p + 0.01) 
        
        if next_p <= limit:
            end_idx += 1
        else:
            break
            
    # 3. Expand backward: include previous hour if it's cheaper OR rises by < 10%
    while start_idx - 1 >= 0:
        curr_p = valid_frames[start_idx]["metrics"]["pricing"]["price_gross"]
        prev_p = valid_frames[start_idx - 1]["metrics"]["pricing"]["price_gross"]
        
        limit = max(curr_p * 1.10, curr_p + 0.01)
        
        if prev_p <= limit:
            start_idx -= 1
        else:
            break
            
    window_frames = valid_frames[start_idx : end_idx + 1]
    net_prices = [f["metrics"]["pricing"]["price_net"] for f in window_frames]
    gross_prices = [f["metrics"]["pricing"]["price_gross"] for f in window_frames]
    
    # Extract all prices for HA attributes
    all_prices = []
    current_price = None
    # Get current hour in UTC for matching
    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    now_iso = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    
    for f in valid_frames:
        p_net = f["metrics"]["pricing"]["price_net"]
        p_gross = f["metrics"]["pricing"]["price_gross"]
        all_prices.append({
            "start": f["start"],
            "end": f["end"],
            "price": p_net,
            "price_gross": p_gross
        })
        # Find price for the current hour
        if f["start"] == now_iso:
            current_price = p_net

    return {
        "start": window_frames[0]["start"],
        "end": window_frames[-1]["end"],
        "duration_hours": len(window_frames),
        "current_price": current_price,
        "all_prices": all_prices,
        "net": {
            "min": round(min(net_prices), 5),
            "max": round(max(net_prices), 5),
            "avg": round(sum(net_prices) / len(net_prices), 5)
        },
        "gross": {
            "min": round(min(gross_prices), 5),
            "max": round(max(gross_prices), 5),
            "avg": round(sum(gross_prices) / len(gross_prices), 5)
        }
    }

def main():
    parser = argparse.ArgumentParser(description="Find cheapest energy hours using Pstryk API.")
    parser.add_argument("--key", help="Pstryk API Key")
    parser.add_argument("--hours", type=int, default=48, help="How many hours to look ahead")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()
    api_key = args.key or os.getenv("PSTRYK_API_KEY") or DEFAULT_API_KEY

    # Debug info (sent as JSON attribute)
    debug_info = {}
    if not api_key or api_key == "sk-YOUR_TOKEN_HERE":
        debug_info["key_status"] = "Missing"
    elif api_key.startswith("!secret"):
        debug_info["key_status"] = "Not Substituted"
    else:
        debug_info["key_status"] = "Present"

    if debug_info["key_status"] != "Present":
        print(json.dumps({"error": f"API Key Issue: {debug_info['key_status']}", "debug": debug_info}))
        return

    data = get_pstryk_data(api_key, args.hours)
    result = find_cheapest_window(data)
    
    # Merge debug info into result
    if isinstance(result, dict):
        result["debug"] = debug_info
        
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
