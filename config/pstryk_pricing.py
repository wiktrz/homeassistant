import requests
from datetime import datetime, timedelta, timezone
import json
import os
import argparse

# --- CONFIGURATION ---
# It's recommended to set this in your environment or a .env file
DEFAULT_API_KEY = "sk-YOUR_TOKEN_HERE"
BASE_URL = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/"

def get_pstryk_data(api_key, hours_ahead=48):
    """
    Fetches pricing data from Pstryk API for the specified window.
    """
    now = datetime.now(timezone.utc)
    # Start from the next hour (snapped)
    window_start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    window_end = (now + timedelta(hours=hours_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')

    params = {
        "metrics": "pricing",
        "resolution": "hour",
        "window_start": window_start,
        "window_end": window_end
    }
    headers = {
        "Authorization": api_key,
        "Accept": "application/json"
    }

    try:
        response = requests.get(BASE_URL, params=params, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        # We don't print to stdout to avoid breaking JSON output in HA
        return {"error": str(e)}

def find_cheapest_window(data):
    """
    Finds the maximum contiguous window around the minimum price.
    Extends the window as long as the price rise between hours is <= 10%.
    """
    if "error" in data:
        return data

    frames = data.get("frames", [])
    if not frames:
        return None

    # Filter out frames that don't have pricing data or have null prices
    valid_frames = [
        f for f in frames 
        if "pricing" in f.get("metrics", {}) and f["metrics"]["pricing"].get("price_net") is not None
    ]
    
    if not valid_frames:
        return None

    # 1. Find the absolute minimum price and its index
    min_f = min(valid_frames, key=lambda x: x["metrics"]["pricing"]["price_net"])
    min_price = min_f["metrics"]["pricing"]["price_net"]
    min_idx = valid_frames.index(min_f)
    
    start_idx = min_idx
    end_idx = min_idx
    
    # 2. Expand forward: include next hour if it's cheaper OR rises by < 10%
    while end_idx + 1 < len(valid_frames):
        curr_p = valid_frames[end_idx]["metrics"]["pricing"]["price_net"]
        next_p = valid_frames[end_idx + 1]["metrics"]["pricing"]["price_net"]
        
        # If price is 0, 10% increase is still 0. We allow a tiny epsilon for 0-price handling.
        limit = max(curr_p * 1.10, curr_p + 0.001) 
        
        if next_p <= limit:
            end_idx += 1
        else:
            break
            
    # 3. Expand backward: include previous hour if it's cheaper OR rises by < 10%
    while start_idx - 1 >= 0:
        curr_p = valid_frames[start_idx]["metrics"]["pricing"]["price_net"]
        prev_p = valid_frames[start_idx - 1]["metrics"]["pricing"]["price_net"]
        
        limit = max(curr_p * 1.10, curr_p + 0.001)
        
        if prev_p <= limit:
            start_idx -= 1
        else:
            break
            
    window_frames = valid_frames[start_idx : end_idx + 1]
    
    # Net prices
    net_prices = [f["metrics"]["pricing"]["price_net"] for f in window_frames]
    avg_net = sum(net_prices) / len(net_prices)
    min_net = min(net_prices)
    max_net = max(net_prices)
    
    # Gross prices
    gross_prices = [f["metrics"]["pricing"]["price_gross"] for f in window_frames]
    avg_gross = sum(gross_prices) / len(gross_prices)
    min_gross = min(gross_prices)
    max_gross = max(gross_prices)
    
    return {
        "start": window_frames[0]["start"],
        "end": window_frames[-1]["end"],
        "duration_hours": len(window_frames),
        "net": {
            "min": round(min_net, 5),
            "max": round(max_net, 5),
            "avg": round(avg_net, 5)
        },
        "gross": {
            "min": round(min_gross, 5),
            "max": round(max_gross, 5),
            "avg": round(avg_gross, 5)
        }
    }

def main():
    parser = argparse.ArgumentParser(description="Find cheapest energy hours using Pstryk API.")
    parser.add_argument("--key", help="Pstryk API Key (overrides PSTRYK_API_KEY env var)")
    parser.add_argument("--hours", type=int, default=48, help="How many hours to look ahead (default: 48)")
    parser.add_argument("--all-cheap", action="store_true", help="Show all hours marked as 'is_cheap'")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    # Priority: CLI Argument > Environment Variable > Hardcoded Default
    api_key = args.key or os.getenv("PSTRYK_API_KEY") or DEFAULT_API_KEY

    if api_key == "sk-YOUR_TOKEN_HERE":
        print(json.dumps({"error": "API Key not set"}))
        return

    data = get_pstryk_data(api_key, args.hours)
    
    if not data or (isinstance(data, dict) and "error" in data):
        print(json.dumps(data or {"error": "No data returned"}))
        return

    if args.all_cheap:
        # If user wants all hours marked as cheap
        result = [f for f in data.get("frames", []) if f.get("metrics", {}).get("pricing", {}).get("is_cheap")]
    else:
        # Default: find the maximum cheap window
        result = find_cheapest_window(data)

    if not result:
        print(json.dumps({"error": "No valid pricing window found"}))
    else:
        print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
