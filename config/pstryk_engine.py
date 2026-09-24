#!/usr/bin/env python3
"""
Pstryk Dynamic Energy Pricing & Metering Engine.
Handles API communication, 15-minute caching lifecycle in /tmp, metric parsing,
open frame null-safety, cheapest window expansion, prosumer price derivation,
and multi-period aggregations (current, today, month, year) for Home Assistant.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

DEFAULT_API_KEY = "sk-YOUR_TOKEN_HERE"
BASE_URL = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/"
CACHE_TTL_SECONDS = 900  # 15 minutes
DEFAULT_CACHE_DIR = "/tmp"


def get_cache_file_path(installation: str = "dol", cache_dir: str = DEFAULT_CACHE_DIR) -> str:
    """Returns absolute path to installation cache file in /tmp."""
    clean_inst = "dol" if installation not in ("dol", "gora") else installation
    return os.path.join(cache_dir, f"pstryk_cache_{clean_inst}.json")


def read_cache(cache_path: str, ttl_seconds: int = CACHE_TTL_SECONDS) -> Optional[Dict[str, Any]]:
    """
    Reads cached JSON data if file exists, is not expired, and contains valid JSON.
    Returns None if cache is missing, expired, or corrupted.
    """
    if not os.path.exists(cache_path):
        return None

    try:
        mtime = os.path.getmtime(cache_path)
        if (time.time() - mtime) > ttl_seconds:
            return None

        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return None

        return data
    except (json.JSONDecodeError, OSError, ValueError):
        # Corrupted cache file: safely discard
        try:
            os.remove(cache_path)
        except OSError:
            pass
        return None


def write_cache(cache_path: str, data: Dict[str, Any]) -> bool:
    """Writes JSON data to cache path safely via an atomic temporary file."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
        tmp_path = f"{cache_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, cache_path)
        return True
    except (OSError, TypeError):
        return False


def fetch_api(url: str, params: Dict[str, Any], api_key: str, timeout: int = 10) -> Dict[str, Any]:
    """Executes a single GET request against the Pstryk API with robust error handling."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "HomeAssistant-PstrykEngine/2.0",
    }
    try:
        req = urllib.request.Request(full_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
            else:
                return {"error": f"HTTP Error {response.status}", "status_code": response.status}
    except urllib.error.HTTPError as e:
        err_msg = f"HTTP Error {e.code}"
        try:
            body = e.read().decode("utf-8")
            parsed = json.loads(body)
            if isinstance(parsed, dict) and ("error" in parsed or "detail" in parsed):
                return {"error": parsed.get("error") or parsed.get("detail"), "status_code": e.code}
        except Exception:
            pass
        return {"error": err_msg, "status_code": e.code}
    except urllib.error.URLError as e:
        return {"error": f"Network Error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def get_pstryk_data(
    api_key: str,
    hours_ahead: int = 48,
    installation: str = "dol",
    use_cache: bool = True,
    cache_ttl: int = CACHE_TTL_SECONDS,
    cache_dir: str = DEFAULT_CACHE_DIR,
    metrics: str = "pricing,cost,meter_values,carbon",
    resolution: str = "hour",
    temporal: Optional[str] = None,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetches raw data from Pstryk unified-metrics API with cache-first lookup and error resilience.
    Supports single queries or legacy calls.
    """
    cache_file = get_cache_file_path(installation, cache_dir=cache_dir)

    # Fast-path for default cache hit
    if use_cache and temporal is None and window_start is None and window_end is None:
        cached = read_cache(cache_file, ttl_seconds=cache_ttl)
        if cached is not None:
            cached["_cache_hit"] = True
            return cached

    now = datetime.now(timezone.utc)
    if temporal:
        params = {
            "temporal": temporal,
            "metrics": metrics,
        }
    else:
        if window_start is None:
            window_start = now.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
        if window_end is None:
            window_end = (now + timedelta(hours=hours_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')

        params = {
            "metrics": metrics,
            "resolution": resolution,
            "window_start": window_start,
            "window_end": window_end,
        }

    payload = fetch_api(BASE_URL, params, api_key)
    if (
        use_cache
        and isinstance(payload, dict)
        and "frames" in payload
        and temporal is None
        and window_start is None
        and window_end is None
    ):
        write_cache(cache_file, payload)
    return payload


def parse_metrics(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parses and standardizes frames from API payload across all 4 metrics:
    pricing, cost, meter_values, and carbon.
    Guarantees null-safety for open/live frames.
    """
    if not data or not isinstance(data, dict):
        return []

    frames = data.get("frames", [])
    if isinstance(frames, dict):
        frames = [{"metrics": frames, "start": None, "end": None, "is_live": False}]

    parsed_frames = []

    for f in frames:
        if not isinstance(f, dict):
            continue

        raw_metrics = f.get("metrics") or {}
        is_live = bool(f.get("is_live", False))

        # 1. Pricing Metric
        raw_pricing = raw_metrics.get("pricing") or {}
        price_net = raw_pricing.get("price_net")
        price_gross = raw_pricing.get("price_gross")
        sell_net = raw_pricing.get("sell_price_net") or raw_pricing.get("price_prosumer_net")
        sell_gross = raw_pricing.get("sell_price_gross") or raw_pricing.get("price_prosumer_gross")

        if sell_gross is None and sell_net is not None:
            sell_gross = round(sell_net * 1.23, 4)

        pricing_obj = {
            "price_net": float(price_net) if price_net is not None else None,
            "price_gross": float(price_gross) if price_gross is not None else None,
            "sell_price_net": float(sell_net) if sell_net is not None else None,
            "sell_price_gross": float(sell_gross) if sell_gross is not None else None,
        }

        # 2. Cost Metric (null-safe)
        raw_cost = raw_metrics.get("cost")
        if raw_cost is not None and isinstance(raw_cost, dict):
            cost_obj = {
                "cost_net": float(raw_cost.get("cost_net", 0.0) or 0.0),
                "cost_gross": float(raw_cost.get("cost_gross", 0.0) or 0.0),
                "sell_net": float(raw_cost.get("sell_net", 0.0) or 0.0),
                "sell_gross": float(raw_cost.get("sell_gross", 0.0) or 0.0),
                "balance": float(raw_cost.get("balance", 0.0) or 0.0),
            }
        else:
            cost_obj = None

        # 3. Meter Values Metric (null-safe)
        raw_mv = raw_metrics.get("meter_values")
        if raw_mv is not None and isinstance(raw_mv, dict):
            mv_obj = {
                "a_plus": float(raw_mv.get("a_plus", 0.0) or 0.0),
                "a_minus": float(raw_mv.get("a_minus", 0.0) or 0.0),
            }
        else:
            mv_obj = None

        # 4. Carbon Metric (null-safe)
        raw_carbon = raw_metrics.get("carbon")
        if raw_carbon is not None and isinstance(raw_carbon, dict):
            carbon_obj = {
                "co2_eq": float(raw_carbon.get("co2_eq", 0.0) or 0.0),
                "factor": float(raw_carbon.get("factor", 0.0) or 0.0),
            }
        else:
            carbon_obj = None

        parsed_frames.append({
            "start": f.get("start"),
            "end": f.get("end"),
            "is_live": is_live,
            "pricing": pricing_obj,
            "cost": cost_obj,
            "meter_values": mv_obj,
            "carbon": carbon_obj,
        })

    return parsed_frames


def extract_prosumer_pricing(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extracts prosumer selling prices (net and gross) for all frames.
    Derives gross from net (23% VAT) if gross is absent.
    """
    parsed = parse_metrics(data)
    results = []
    for frame in parsed:
        p = frame.get("pricing") or {}
        results.append({
            "start": frame.get("start"),
            "end": frame.get("end"),
            "sell_price_net": p.get("sell_price_net"),
            "sell_price_gross": p.get("sell_price_gross"),
        })
    return results


def find_cheapest_window(data: Dict[str, Any], max_increase_ratio: float = 0.10) -> Dict[str, Any]:
    """
    Finds the optimal contiguous window around the minimum price.
    Expands forward and backward as long as adjacent prices rise <= 10%.
    """
    if not data or "error" in data:
        return data

    frames = data.get("frames", [])
    if not frames:
        return {"error": "No frames in data"}

    valid_frames = [
        f for f in frames
        if isinstance(f, dict)
        and "pricing" in (f.get("metrics") or {})
        and (
            (f.get("metrics") or {}).get("pricing", {}).get("price_gross") is not None
            or (f.get("metrics") or {}).get("pricing", {}).get("price_net") is not None
        )
    ]

    if not valid_frames:
        return {"error": "No valid pricing data in frames"}

    # Use gross price for evaluation, fallback to net
    def get_gross(frame):
        p = frame["metrics"]["pricing"]
        if p.get("price_gross") is not None:
            return p["price_gross"]
        if p.get("full_price") is not None:
            return p["full_price"]
        return p.get("price_net", 999.0)

    def get_net(frame):
        p = frame["metrics"]["pricing"]
        if p.get("price_net") is not None:
            return p["price_net"]
        if p.get("tge_price") is not None:
            return p["tge_price"]
        return p.get("price_gross", 999.0)

    # 1. Find the absolute minimum GROSS price and its index
    min_f = min(valid_frames, key=get_gross)
    min_idx = valid_frames.index(min_f)

    start_idx = min_idx
    end_idx = min_idx

    # 2. Expand forward: include next hour if it's cheaper OR rises by <= 10%
    while end_idx + 1 < len(valid_frames):
        curr_p = get_gross(valid_frames[end_idx])
        next_p = get_gross(valid_frames[end_idx + 1])
        limit = max(curr_p * (1.0 + max_increase_ratio), curr_p + 0.01)

        if next_p <= limit:
            end_idx += 1
        else:
            break

    # 3. Expand backward: include previous hour if it's cheaper OR rises by <= 10%
    while start_idx - 1 >= 0:
        curr_p = get_gross(valid_frames[start_idx])
        prev_p = get_gross(valid_frames[start_idx - 1])
        limit = max(curr_p * (1.0 + max_increase_ratio), curr_p + 0.01)

        if prev_p <= limit:
            start_idx -= 1
        else:
            break

    window_frames = valid_frames[start_idx: end_idx + 1]
    net_prices = [get_net(f) for f in window_frames]
    gross_prices = [get_gross(f) for f in window_frames]

    # Extract all prices for attributes
    all_prices = []
    current_price = None
    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    now_iso = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

    for f in valid_frames:
        p_net = get_net(f)
        p_gross = get_gross(f)
        all_prices.append({
            "start": f.get("start"),
            "end": f.get("end"),
            "price": p_net,
            "price_gross": p_gross,
        })
        if f.get("start") == now_iso:
            current_price = p_gross

    # If current hour was not exactly matched in windows, fallback to first valid frame
    if current_price is None and valid_frames:
        current_price = get_gross(valid_frames[0])

    return {
        "start": window_frames[0].get("start"),
        "end": window_frames[-1].get("end"),
        "duration_hours": len(window_frames),
        "current_price": current_price,
        "all_prices": all_prices,
        "net": {
            "min": round(min(net_prices), 5),
            "max": round(max(net_prices), 5),
            "avg": round(sum(net_prices) / len(net_prices), 5),
        },
        "gross": {
            "min": round(min(gross_prices), 5),
            "max": round(max(gross_prices), 5),
            "avg": round(sum(gross_prices) / len(gross_prices), 5),
        },
    }


def aggregate_frames(data: Dict[str, Any]) -> Dict[str, float]:
    """
    Computes total energy bought/sold (kWh), total cost, earned revenue (PLN),
    and balance across all frames. Matches API summary contract.
    """
    parsed = parse_metrics(data)
    total_bought = 0.0
    total_sold = 0.0
    total_cost = 0.0
    total_earned = 0.0

    for f in parsed:
        mv = f.get("meter_values")
        if mv:
            total_bought += mv.get("a_plus", 0.0) or 0.0
            total_sold += mv.get("a_minus", 0.0) or 0.0

        cost = f.get("cost")
        if cost:
            total_cost += cost.get("cost_gross", 0.0) or 0.0
            total_earned += cost.get("sell_gross", 0.0) or 0.0

    balance = total_cost - total_earned

    return {
        "total_kwh_bought": round(total_bought, 2),
        "total_kwh_sold": round(total_sold, 2),
        "total_cost_pln": round(total_cost, 2),
        "total_earned_pln": round(total_earned, 2),
        "balance_pln": round(balance, 2),
    }


def format_hhmm(iso_str: Optional[str]) -> str:
    """Extracts 'HH:MM' time string from an ISO timestamp."""
    if not iso_str or not isinstance(iso_str, str):
        return "--:--"
    if "T" in iso_str:
        time_part = iso_str.split("T")[1]
        return time_part[:5]
    return iso_str[:5]


def build_current_subobject(latest_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds the 'current' sub-object from temporal=latest (or fallback live frame).
    Schema:
    {
      "price_gross": ..., "price_net": ..., "sell_price_gross": ..., "sell_price_net": ...,
      "tge_price": ..., "dist_price": ..., "service_price": ..., "vat": ..., "is_cheap": bool, "is_expensive": bool,
      "instant_kwh_import": ..., "carbon_g": ...
    }
    """
    if not latest_data or not isinstance(latest_data, dict):
        return {
            "price_gross": 0.0, "price_net": 0.0, "sell_price_gross": 0.0, "sell_price_net": 0.0,
            "tge_price": 0.0, "dist_price": 0.0, "service_price": 0.0, "vat": 0.0,
            "is_cheap": False, "is_expensive": False, "instant_kwh_import": 0.0, "carbon_g": 0.0,
        }

    frames = latest_data.get("frames", {})
    if isinstance(frames, dict):
        metrics = frames
    elif isinstance(frames, list) and frames:
        live = [f for f in frames if isinstance(f, dict) and f.get("is_live")]
        chosen = live[-1] if live else frames[-1]
        metrics = chosen.get("metrics") or chosen if isinstance(chosen, dict) else {}
    else:
        metrics = {}

    pricing = metrics.get("pricing") or {}
    cost = metrics.get("cost") or {}
    mv = metrics.get("meter_values") or {}
    carbon = metrics.get("carbon") or {}

    p_gross = pricing.get("price_gross") if pricing.get("price_gross") is not None else pricing.get("full_price")
    p_net = pricing.get("price_net") if pricing.get("price_net") is not None else pricing.get("tge_price")
    if p_gross is None and p_net is not None:
        p_gross = round(p_net * 1.23, 4)

    sp_gross = pricing.get("price_prosumer_gross") if pricing.get("price_prosumer_gross") is not None else pricing.get("sell_price_gross")
    sp_net = pricing.get("price_prosumer_net") if pricing.get("price_prosumer_net") is not None else pricing.get("sell_price_net")
    if sp_gross is None and sp_net is not None:
        sp_gross = round(sp_net * 1.23, 4)

    vat = pricing.get("vat_component") if pricing.get("vat_component") is not None else cost.get("vat")
    if vat is None and p_gross is not None and p_net is not None:
        vat = round(p_gross - p_net, 4)

    ik = mv.get("energy_active_import_register") if mv.get("energy_active_import_register") is not None else mv.get("a_plus")
    cg = carbon.get("carbon_footprint") if carbon.get("carbon_footprint") is not None else carbon.get("co2_eq")

    return {
        "price_gross": round(float(p_gross), 4) if p_gross is not None else 0.0,
        "price_net": round(float(p_net), 4) if p_net is not None else 0.0,
        "sell_price_gross": round(float(sp_gross), 4) if sp_gross is not None else 0.0,
        "sell_price_net": round(float(sp_net), 4) if sp_net is not None else 0.0,
        "tge_price": round(float(pricing.get("tge_price") if pricing.get("tge_price") is not None else (p_net or 0.0)), 4),
        "dist_price": round(float(pricing.get("dist_price", 0.0) or 0.0), 4),
        "service_price": round(float(pricing.get("service_price", 0.0) or 0.0), 4),
        "vat": round(float(vat or 0.0), 4),
        "is_cheap": bool(pricing.get("is_cheap", False)),
        "is_expensive": bool(pricing.get("is_expensive", False)),
        "instant_kwh_import": round(float(ik or 0.0), 4),
        "carbon_g": round(float(cg or 0.0), 2),
    }


def build_today_subobject(today_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds the 'today' sub-object including summary aggregations and hourly breakdown.
    Schema:
    {
      "kwh_import": ..., "kwh_export": ..., "cost_pln": ..., "earned_pln": ..., "balance_pln": ..., "carbon_g": ...,
      "hourly": [{"start": "HH:MM", "end": "HH:MM", "kwh": ..., "cost_pln": ..., "rate_gross": ...}, ...]
    }
    """
    if not today_data or not isinstance(today_data, dict):
        return {
            "kwh_import": 0.0, "kwh_export": 0.0, "cost_pln": 0.0, "earned_pln": 0.0,
            "balance_pln": 0.0, "carbon_g": 0.0, "hourly": [],
        }

    frames = today_data.get("frames", [])
    if not isinstance(frames, list):
        frames = []

    summary = today_data.get("summary") or {}
    mv_summary = summary.get("meter_values") or {}
    cost_summary = summary.get("cost") or {}
    carb_summary = summary.get("carbon") or {}

    kwh_imp = mv_summary.get("energy_active_import_register_total")
    if kwh_imp is None:
        kwh_imp = summary.get("total_kwh_bought")
    if kwh_imp is None:
        kwh_imp = sum(
            ((f.get("metrics") or {}).get("meter_values") or {}).get("energy_active_import_register", 0.0) or
            ((f.get("metrics") or {}).get("meter_values") or {}).get("a_plus", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    kwh_exp = mv_summary.get("energy_active_export_register_total")
    if kwh_exp is None:
        kwh_exp = summary.get("total_kwh_sold")
    if kwh_exp is None:
        kwh_exp = sum(
            ((f.get("metrics") or {}).get("meter_values") or {}).get("energy_active_export_register", 0.0) or
            ((f.get("metrics") or {}).get("meter_values") or {}).get("a_minus", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    cost_p = (
        cost_summary.get("total_energy_import_cost")
        or cost_summary.get("total_fae_cost")
        or summary.get("total_cost_pln")
    )
    if cost_p is None:
        cost_p = sum(
            ((f.get("metrics") or {}).get("cost") or {}).get("energy_import_cost", 0.0) or
            ((f.get("metrics") or {}).get("cost") or {}).get("cost_gross", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    earned_p = cost_summary.get("total_energy_sold_value") or summary.get("total_earned_pln")
    if earned_p is None:
        earned_p = sum(
            ((f.get("metrics") or {}).get("cost") or {}).get("energy_sold_value", 0.0) or
            ((f.get("metrics") or {}).get("cost") or {}).get("sell_gross", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    balance_p = cost_summary.get("total_energy_balance_value") or summary.get("balance_pln")
    if balance_p is None:
        balance_p = (cost_p or 0.0) - (earned_p or 0.0)

    carb_g = carb_summary.get("carbon_footprint_total")
    if carb_g is None:
        carb_g = sum(
            ((f.get("metrics") or {}).get("carbon") or {}).get("carbon_footprint", 0.0) or
            ((f.get("metrics") or {}).get("carbon") or {}).get("co2_eq", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    hourly = []
    for f in frames:
        if not isinstance(f, dict):
            continue
        m = f.get("metrics") or {}
        mv = m.get("meter_values") or {}
        c = m.get("cost") or {}
        p = m.get("pricing") or {}

        k = mv.get("energy_active_import_register") if mv.get("energy_active_import_register") is not None else mv.get("a_plus")
        cp = c.get("energy_import_cost") if c.get("energy_import_cost") is not None else c.get("cost_gross")
        rg = p.get("price_gross") if p.get("price_gross") is not None else p.get("full_price")

        hourly.append({
            "start": format_hhmm(f.get("start")),
            "end": format_hhmm(f.get("end")),
            "kwh": round(float(k), 4) if k is not None else 0.0,
            "cost_pln": round(float(cp), 2) if cp is not None else 0.0,
            "rate_gross": round(float(rg), 4) if rg is not None else 0.0,
        })

    return {
        "kwh_import": round(float(kwh_imp or 0.0), 3),
        "kwh_export": round(float(kwh_exp or 0.0), 3),
        "cost_pln": round(float(cost_p or 0.0), 2),
        "earned_pln": round(float(earned_p or 0.0), 2),
        "balance_pln": round(float(balance_p or 0.0), 2),
        "carbon_g": round(float(carb_g or 0.0), 1),
        "hourly": hourly,
    }


def build_month_subobject(month_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds the 'month' sub-object including summary aggregations and daily breakdown.
    Schema:
    {
      "kwh_import": ..., "kwh_export": ..., "cost_pln": ..., "earned_pln": ..., "balance_pln": ...,
      "daily": [{"date": "YYYY-MM-DD", "kwh": ..., "cost_pln": ..., "avg_rate": ...}, ...]
    }
    """
    if not month_data or not isinstance(month_data, dict):
        return {
            "kwh_import": 0.0, "kwh_export": 0.0, "cost_pln": 0.0, "earned_pln": 0.0,
            "balance_pln": 0.0, "daily": [],
        }

    frames = month_data.get("frames", [])
    if not isinstance(frames, list):
        frames = []

    summary = month_data.get("summary") or {}
    mv_summary = summary.get("meter_values") or {}
    cost_summary = summary.get("cost") or {}

    kwh_imp = mv_summary.get("energy_active_import_register_total")
    if kwh_imp is None:
        kwh_imp = summary.get("total_kwh_bought")
    if kwh_imp is None:
        kwh_imp = sum(
            ((f.get("metrics") or {}).get("meter_values") or {}).get("energy_active_import_register", 0.0) or
            ((f.get("metrics") or {}).get("meter_values") or {}).get("a_plus", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    kwh_exp = mv_summary.get("energy_active_export_register_total")
    if kwh_exp is None:
        kwh_exp = summary.get("total_kwh_sold")
    if kwh_exp is None:
        kwh_exp = sum(
            ((f.get("metrics") or {}).get("meter_values") or {}).get("energy_active_export_register", 0.0) or
            ((f.get("metrics") or {}).get("meter_values") or {}).get("a_minus", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    cost_p = (
        cost_summary.get("total_energy_import_cost")
        or cost_summary.get("total_fae_cost")
        or summary.get("total_cost_pln")
    )
    if cost_p is None:
        cost_p = sum(
            ((f.get("metrics") or {}).get("cost") or {}).get("energy_import_cost", 0.0) or
            ((f.get("metrics") or {}).get("cost") or {}).get("cost_gross", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    earned_p = cost_summary.get("total_energy_sold_value") or summary.get("total_earned_pln")
    if earned_p is None:
        earned_p = sum(
            ((f.get("metrics") or {}).get("cost") or {}).get("energy_sold_value", 0.0) or
            ((f.get("metrics") or {}).get("cost") or {}).get("sell_gross", 0.0) or 0.0
            for f in frames if isinstance(f, dict)
        )

    balance_p = cost_summary.get("total_energy_balance_value") or summary.get("balance_pln")
    if balance_p is None:
        balance_p = (cost_p or 0.0) - (earned_p or 0.0)

    daily = []
    for f in frames:
        if not isinstance(f, dict):
            continue
        m = f.get("metrics") or {}
        mv = m.get("meter_values") or {}
        c = m.get("cost") or {}
        p = m.get("pricing") or {}

        k = mv.get("energy_active_import_register") if mv.get("energy_active_import_register") is not None else mv.get("a_plus")
        cp = c.get("energy_import_cost") if c.get("energy_import_cost") is not None else c.get("cost_gross")
        rg = p.get("price_gross") if p.get("price_gross") is not None else p.get("full_price") or p.get("price_gross_avg")

        st = f.get("start") or ""
        date_str = st[:10] if len(st) >= 10 else st

        daily.append({
            "date": date_str,
            "kwh": round(float(k), 3) if k is not None else 0.0,
            "cost_pln": round(float(cp), 2) if cp is not None else 0.0,
            "avg_rate": round(float(rg), 4) if rg is not None else 0.0,
        })

    return {
        "kwh_import": round(float(kwh_imp or 0.0), 3),
        "kwh_export": round(float(kwh_exp or 0.0), 3),
        "cost_pln": round(float(cost_p or 0.0), 2),
        "earned_pln": round(float(earned_p or 0.0), 2),
        "balance_pln": round(float(balance_p or 0.0), 2),
        "daily": daily,
    }


def build_year_subobject(year_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds the 'year' sub-object with monthly breakdown.
    Schema:
    {
      "monthly": [{"month": "YYYY-MM", "kwh": ..., "cost_pln": ...}, ...]
    }
    """
    if not year_data or not isinstance(year_data, dict):
        return {"monthly": []}

    frames = year_data.get("frames", [])
    if not isinstance(frames, list):
        frames = []

    monthly = []
    for f in frames:
        if not isinstance(f, dict):
            continue
        m = f.get("metrics") or {}
        mv = m.get("meter_values") or {}
        c = m.get("cost") or {}

        k = mv.get("energy_active_import_register") if mv.get("energy_active_import_register") is not None else mv.get("a_plus")
        cp = c.get("energy_import_cost") if c.get("energy_import_cost") is not None else c.get("cost_gross")

        st = f.get("start") or ""
        mon_str = st[:7] if len(st) >= 7 else st

        monthly.append({
            "month": mon_str,
            "kwh": round(float(k), 2) if k is not None else 0.0,
            "cost_pln": round(float(cp), 2) if cp is not None else 0.0,
        })

    return {"monthly": monthly}


def fetch_consolidated_data(
    api_key: str,
    installation: str = "dol",
    hours_ahead: int = 48,
    use_cache: bool = True,
    cache_ttl: int = CACHE_TTL_SECONDS,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> Dict[str, Any]:
    """
    Fetches all 5 datasets (latest, forward 48h, today, month, year),
    applies smart 15-min file caching in /tmp/pstryk_cache_{installation}.json,
    computes cheapest window, and returns full consolidated payload backward-compatible
    with existing Home Assistant sensors.
    """
    cache_file = get_cache_file_path(installation, cache_dir=cache_dir)

    # 1. Smart cache lookup
    if use_cache:
        cached = read_cache(cache_file, ttl_seconds=cache_ttl)
        if (
            cached is not None
            and isinstance(cached, dict)
            and "current" in cached
            and "today" in cached
            and "all_prices" in cached
        ):
            cached["_cache_hit"] = True
            return cached

    # Check for historical month/year frames from prior cache to preserve during transient outages
    historical_month = None
    historical_year = None
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                old_cached = json.load(f)
            if isinstance(old_cached, dict):
                historical_month = old_cached.get("month")
                historical_year = old_cached.get("year")
        except Exception:
            pass

    # 2. Prepare time boundaries
    now = datetime.now(timezone.utc)
    now_hour = now.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    now_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    month_start_utc = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    year_start_utc = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    forward_end = (now + timedelta(hours=hours_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # 3. Fetch all 5 queries
    latest_data = get_pstryk_data(
        api_key=api_key,
        installation=installation,
        temporal="latest",
        metrics="pricing,cost,meter_values,carbon",
        use_cache=False,
        cache_dir=cache_dir,
    )

    # If critical authentication or bad request error, abort early
    if isinstance(latest_data, dict) and latest_data.get("status_code") in (400, 401):
        return latest_data

    forward_data = get_pstryk_data(
        api_key=api_key,
        installation=installation,
        hours_ahead=hours_ahead,
        window_start=now_hour,
        window_end=forward_end,
        resolution="hour",
        metrics="pricing",
        use_cache=False,
        cache_dir=cache_dir,
    )

    today_data = get_pstryk_data(
        api_key=api_key,
        installation=installation,
        window_start=midnight_utc,
        window_end=now_iso,
        resolution="hour",
        metrics="meter_values,cost,pricing,carbon",
        use_cache=False,
        cache_dir=cache_dir,
    )

    month_data = get_pstryk_data(
        api_key=api_key,
        installation=installation,
        window_start=month_start_utc,
        window_end=now_iso,
        resolution="day",
        metrics="meter_values,cost,pricing",
        use_cache=False,
        cache_dir=cache_dir,
    )

    year_data = get_pstryk_data(
        api_key=api_key,
        installation=installation,
        window_start=year_start_utc,
        window_end=now_iso,
        resolution="month",
        metrics="meter_values,cost,pricing",
        use_cache=False,
        cache_dir=cache_dir,
    )

    # 4. Compute cheapest window & top-level pricing curves
    window_result = find_cheapest_window(forward_data)
    if isinstance(window_result, dict) and "error" in window_result:
        # Fallback to today_data if forward pricing has no frames
        alt_window = find_cheapest_window(today_data)
        if isinstance(alt_window, dict) and "error" not in alt_window:
            window_result = alt_window
        else:
            window_result = {
                "start": None,
                "end": None,
                "duration_hours": 0,
                "current_price": 0.0,
                "all_prices": [],
                "net": {"min": 0.0, "max": 0.0, "avg": 0.0},
                "gross": {"min": 0.0, "max": 0.0, "avg": 0.0},
            }

    # 5. Build rich structured sub-objects
    current_obj = build_current_subobject(latest_data)
    today_obj = build_today_subobject(today_data)

    if isinstance(month_data, dict) and "frames" in month_data and month_data.get("frames"):
        month_obj = build_month_subobject(month_data)
    elif historical_month:
        month_obj = historical_month
    else:
        month_obj = build_month_subobject(month_data)

    if isinstance(year_data, dict) and "frames" in year_data and year_data.get("frames"):
        year_obj = build_year_subobject(year_data)
    elif historical_year:
        year_obj = historical_year
    else:
        year_obj = build_year_subobject(year_data)

    # Ensure top-level current_price is valid float
    curr_price = window_result.get("current_price")
    if curr_price is None:
        curr_price = current_obj.get("price_gross")
    if curr_price is None and window_result.get("all_prices"):
        curr_price = window_result["all_prices"][0].get("price_gross")
    if curr_price is None:
        curr_price = 0.0
    curr_price = round(float(curr_price), 4)

    consolidated = {
        "current_price": curr_price,
        "start": window_result.get("start"),
        "end": window_result.get("end"),
        "duration_hours": window_result.get("duration_hours", 0),
        "all_prices": window_result.get("all_prices", []),
        "net": window_result.get("net", {"min": 0.0, "max": 0.0, "avg": 0.0}),
        "gross": window_result.get("gross", {"min": 0.0, "max": 0.0, "avg": 0.0}),
        "installation": installation,
        "current": current_obj,
        "today": today_obj,
        "month": month_obj,
        "year": year_obj,
        "_cached_at": time.time(),
        "frames": forward_data.get("frames", []),
    }

    if use_cache:
        write_cache(cache_file, consolidated)

    return consolidated


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Pstryk Dynamic Energy Engine")
    parser.add_argument("--key", help="Pstryk API Key")
    parser.add_argument("--hours", type=int, default=48, help="Look-ahead window in hours")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    parser.add_argument("--installation", choices=["dol", "gora"], default="dol", help="Target installation")
    parser.add_argument("--no-cache", action="store_true", help="Bypass file cache")

    args = parser.parse_args(argv)
    api_key = args.key or os.getenv("PSTRYK_API_KEY") or DEFAULT_API_KEY

    debug_info = {}
    if not api_key or api_key == "sk-YOUR_TOKEN_HERE":
        debug_info["key_status"] = "Missing"
    elif api_key.startswith("!secret"):
        debug_info["key_status"] = "Not Substituted"
    else:
        debug_info["key_status"] = "Present"

    if debug_info["key_status"] != "Present":
        err_res = {"error": f"API Key Issue: {debug_info['key_status']}", "debug": debug_info}
        print(json.dumps(err_res, indent=2))
        return 1

    result = fetch_consolidated_data(
        api_key=api_key,
        installation=args.installation,
        hours_ahead=args.hours,
        use_cache=not args.no_cache,
    )

    if isinstance(result, dict):
        result["debug"] = debug_info
        result["installation"] = args.installation

    print(json.dumps(result, indent=2))
    return 0 if (isinstance(result, dict) and "error" not in result) else 1


if __name__ == "__main__":
    sys.exit(main())
