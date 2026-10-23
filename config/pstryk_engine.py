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
import itertools
import math
import statistics
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
try:
    import zoneinfo
    WARSAW_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")
except (ImportError, AttributeError):
    try:
        from backports import zoneinfo  # type: ignore
        WARSAW_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")
    except (ImportError, AttributeError):
        try:
            from dateutil import tz as dateutil_tz  # type: ignore
            WARSAW_TZ = dateutil_tz.gettz("Europe/Warsaw") or timezone(timedelta(hours=2))
        except (ImportError, AttributeError):
            WARSAW_TZ = timezone(timedelta(hours=2))
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import energy_defaults

DEFAULT_API_KEY = "sk-YOUR_TOKEN_HERE"
BASE_URL = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/"
CACHE_TTL_SECONDS = int(energy_defaults.PSTRYK_CACHE_TTL)
DEFAULT_CACHE_DIR = "/tmp"


def parse_target_hours(value: Any, default: Any) -> Any:
    """
    Parses a target-hours setting as passed from HA input_select ("3h", "Auto 2h/4h") or CLI ("3", "auto").
    Returns "auto" for values starting with "auto" when default is "auto", else a positive int (fallback: default).
    """
    s = str(value).strip() if value is not None else ""
    digits = ""
    for ch in s:
        if not ch.isdigit():
            break
        digits += ch
    if digits and int(digits) > 0:
        return int(digits)
    return default


def set_charge_current_with_fallback(host: Optional[str], port: Optional[int], current_a: int) -> Dict[str, Any]:
    """
    Writes Reg 108 via the local Solarman client; falls back to Deye Cloud only when the local client
    is unavailable/raised, or explicitly reports cloud_attempted=False. solarman_v5_client.set_charge_current
    already tries the cloud internally on failure, so a plain success=False is not retried (cloud at most once).
    """
    res: Dict[str, Any] = {"success": False, "error": "solarman_v5_client module not available", "cloud_attempted": False}
    if solarman_v5_client is not None:
        try:
            res = solarman_v5_client.set_charge_current(host=host, port=port, current_a=current_a)
        except Exception as e:
            res = {"success": False, "error": str(e), "cloud_attempted": False}
    if not isinstance(res, dict):
        res = {"success": False, "error": f"Unexpected response: {res!r}", "cloud_attempted": False}
    if not res.get("success") and res.get("cloud_attempted", True) is False:
        try:
            import deye_cloud_client
            cloud_res = deye_cloud_client.set_charge_current_cloud(current_a=current_a)
            if isinstance(cloud_res, dict):
                cloud_res["local_fallback_reason"] = res.get("error")
                res = cloud_res
        except Exception as e:
            res["cloud_error"] = str(e)
    return res


def load_dotenv(paths: Optional[List[str]] = None) -> None:
    """Zero-dependency .env file loader for environment configuration."""
    if paths is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        paths = ["/config/.env", os.path.join(script_dir, ".env")]
    for p in paths:
        if os.path.exists(p) and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


def load_secrets_yaml_key(installation: str = "dol") -> Optional[str]:
    """Fallback reader for secrets.yaml when .env or CLI arguments are not provided."""
    clean_inst = "dol" if installation not in ("dol", "gora") else installation
    target_key = f"pstryk_api_key_{clean_inst}"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(script_dir, ".."))
    paths = [
        "/config/secrets.yaml",
        os.path.join(script_dir, "secrets.yaml"),
        os.path.join(repo_root, "config", "secrets.yaml"),
        os.path.join(repo_root, "secrets.yaml"),
    ]
    for p in paths:
        if os.path.exists(p) and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(target_key):
                            parts = line.split(":", 1)
                            if len(parts) == 2:
                                val = parts[1].strip().strip("'\"")
                                if val and not val.startswith("!") and not val.startswith("sk-YOUR_"):
                                    return val
            except Exception:
                pass
    return None


def resolve_api_key(
    installation: str = "dol",
    explicit_key: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """
    Resolves the Pstryk API key for the given installation.
    Priority:
    1. explicit_key if provided via CLI, not empty, and not placeholder/unsubstituted secret
    2. Environment variable from .env: PSTRYK_API_KEY_{DOL/GORA}
    3. Generic environment variable from .env: PSTRYK_API_KEY
    4. Fallback to secrets.yaml if present: pstryk_api_key_{dol/gora}
    
    Returns: (resolved_key, source_status)
    where source_status can be: "env", "arg", "secrets_yaml", "missing"
    """
    clean_inst = "dol" if str(installation).lower() not in ("dol", "gora") else str(installation).lower()

    def is_valid_candidate(key: Optional[str]) -> bool:
        if not key or not isinstance(key, str):
            return False
        clean = key.strip()
        if clean in (DEFAULT_API_KEY, "None", "", "null"):
            return False
        if clean.startswith("!secret") or clean.startswith("sk-YOUR_") or clean.startswith("sk-xxxx") or clean.startswith("sk-XXXX"):
            return False
        if len(clean) < 8:
            return False
        return True

    # 1. Explicit key argument
    if is_valid_candidate(explicit_key):
        return explicit_key.strip(), "arg"

    # 2. Installation-specific env var from .env
    inst_env_var = f"PSTRYK_API_KEY_{clean_inst.upper()}"
    env_inst_key = os.getenv(inst_env_var)
    if is_valid_candidate(env_inst_key):
        return env_inst_key.strip(), "env"

    # 3. Generic env var from .env
    generic_env = os.getenv("PSTRYK_API_KEY")
    if is_valid_candidate(generic_env):
        return generic_env.strip(), "env"

    # 4. Fallback: secrets.yaml
    sec_key = load_secrets_yaml_key(clean_inst)
    if is_valid_candidate(sec_key):
        return sec_key.strip(), "secrets_yaml"

    return None, "missing"


load_dotenv()

try:
    import solarman_v5_client
except ImportError:
    solarman_v5_client = None



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
        return p.get("price_net", energy_defaults.PRICE_MISSING_SENTINEL)

    def get_net(frame):
        p = frame["metrics"]["pricing"]
        if p.get("price_net") is not None:
            return p["price_net"]
        if p.get("tge_price") is not None:
            return p["tge_price"]
        return p.get("price_gross", energy_defaults.PRICE_MISSING_SENTINEL)

    # 1. Find the absolute minimum GROSS price and its index
    min_f = min(valid_frames, key=get_gross)
    min_idx = valid_frames.index(min_f)

    start_idx = min_idx
    end_idx = min_idx

    # 2. Expand forward: include next hour if it's cheaper OR rises by <= 10%
    while end_idx + 1 < len(valid_frames):
        curr_p = get_gross(valid_frames[end_idx])
        next_p = get_gross(valid_frames[end_idx + 1])
        if curr_p > 0:
            limit = max(curr_p * (1.0 + max_increase_ratio), curr_p + 0.01)
        else:
            limit = curr_p + 0.05

        if next_p <= limit:
            end_idx += 1
        else:
            break

    # 3. Expand backward: include previous hour if it's cheaper OR rises by <= 10%
    while start_idx - 1 >= 0:
        curr_p = get_gross(valid_frames[start_idx])
        prev_p = get_gross(valid_frames[start_idx - 1])
        if curr_p > 0:
            limit = max(curr_p * (1.0 + max_increase_ratio), curr_p + 0.01)
        else:
            limit = curr_p + 0.05

        if prev_p <= limit:
            start_idx -= 1
        else:
            break

    window_frames = valid_frames[start_idx: end_idx + 1]
    net_prices = [get_net(f) for f in window_frames]
    gross_prices = [get_gross(f) for f in window_frames]
    avg_gross = round(sum(gross_prices) / len(gross_prices), 3)

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

    start_str = window_frames[0].get("start")
    end_str = window_frames[-1].get("end")
    start_fmt = format_hhmm(start_str)
    end_fmt = format_hhmm(end_str)
    display = f"{start_fmt} - {end_fmt} (śr. {avg_gross:.2f} zł/kWh)"

    return {
        "start": start_str,
        "end": end_str,
        "duration_hours": len(window_frames),
        "current_price": current_price,
        "average_price": avg_gross,
        "display": display,
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


def resolve_battery_hardware_parameters(
    battery_capacity_kwh: Optional[float] = None,
    nominal_voltage: Optional[float] = None,
    max_charge_current: Optional[float] = None,
    max_discharge_current: Optional[float] = None,
    inv_cfg: Optional[Dict[str, Any]] = None,
    inv_tlm: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float, float, float]:
    """
    Dynamically resolves physical battery capacity (kWh), nominal voltage (V),
    maximum charge current (A), and maximum discharge current (A) without hardcoding static constants.
    Priority:
    1. Explicit arguments (if provided and > 0)
    2. Inverter config / telemetry dictionary (if provided)
    3. Cached inverter readings (from solarman_v5_client cache)
    4. Environment variables (BATTERY_CAPACITY_KWH, BATTERY_NOMINAL_VOLTAGE, BATTERY_MAX_CHARGE_CURRENT, BATTERY_MAX_DISCHARGE_CURRENT)
    5. Fallback defaults from .env / energy_defaults ONLY if completely unconfigured anywhere
    """
    cfg = dict(inv_cfg) if inv_cfg else {}
    tlm = dict(inv_tlm) if inv_tlm else {}

    # Check cached inverter data if not supplied
    if not cfg:
        try:
            import solarman_v5_client
            cached = solarman_v5_client.load_deye_cache(ignore_ttl=True)
            if cached and isinstance(cached, dict):
                cfg = cached.get("config", {}) or {}
                tlm = cached.get("telemetry", {}) or {}
        except Exception:
            pass

    # 1. Resolve Nominal Voltage (V)
    resolved_v = None
    if nominal_voltage is not None and float(nominal_voltage) > 0:
        resolved_v = float(nominal_voltage)
    elif cfg.get("nominal_voltage") and float(cfg["nominal_voltage"]) > 0:
        resolved_v = float(cfg["nominal_voltage"])
    elif tlm.get("battery_voltage") and float(tlm["battery_voltage"]) > energy_defaults.BATTERY_PRESENCE_VOLTAGE_THRESHOLD:
        b_v = float(tlm["battery_voltage"])
        resolved_v = energy_defaults.FALLBACK_BATTERY_NOMINAL_VOLTAGE if energy_defaults.LIFEPO4_16S_VOLTAGE_LOW <= b_v <= energy_defaults.LIFEPO4_16S_VOLTAGE_HIGH else b_v
    else:
        env_v = os.getenv("BATTERY_NOMINAL_VOLTAGE")
        resolved_v = float(env_v) if env_v else energy_defaults.FALLBACK_BATTERY_NOMINAL_VOLTAGE

    # 2. Resolve Maximum Charge Current (A)
    resolved_curr = None
    if max_charge_current is not None and float(max_charge_current) > 0:
        resolved_curr = float(max_charge_current)
    elif cfg.get("max_charge_current") and float(cfg["max_charge_current"]) > 0:
        resolved_curr = float(cfg["max_charge_current"])
    else:
        env_c = os.getenv("BATTERY_MAX_CHARGE_CURRENT") or os.getenv("BATTERY_CHARGE_CURRENT_A")
        resolved_curr = float(env_c) if env_c else energy_defaults.FALLBACK_BATTERY_MAX_CHARGE_CURRENT

    # 3. Resolve Maximum Discharge Current (A)
    resolved_dis_curr = None
    if max_discharge_current is not None and float(max_discharge_current) > 0:
        resolved_dis_curr = float(max_discharge_current)
    elif cfg.get("max_discharge_current") and float(cfg["max_discharge_current"]) > 0:
        resolved_dis_curr = float(cfg["max_discharge_current"])
    else:
        env_dc = os.getenv("BATTERY_MAX_DISCHARGE_CURRENT") or os.getenv("BATTERY_DISCHARGE_CURRENT_A")
        resolved_dis_curr = float(env_dc) if env_dc else energy_defaults.FALLBACK_BATTERY_MAX_DISCHARGE_CURRENT

    # 4. Resolve Battery Capacity (kWh)
    resolved_cap = None
    if battery_capacity_kwh is not None and float(battery_capacity_kwh) > 0:
        resolved_cap = float(battery_capacity_kwh)
    elif cfg.get("batt_capacity_kwh") and float(cfg["batt_capacity_kwh"]) > 0:
        resolved_cap = float(cfg["batt_capacity_kwh"])
    elif cfg.get("batt_capacity") and float(cfg["batt_capacity"]) > 0:
        resolved_cap = round((float(cfg["batt_capacity"]) * resolved_v) / 1000.0, 2)
    else:
        env_cap = os.getenv("BATTERY_CAPACITY_KWH")
        resolved_cap = float(env_cap) if env_cap else energy_defaults.FALLBACK_BATTERY_CAPACITY_KWH

    return resolved_cap, resolved_v, resolved_curr, resolved_dis_curr


# Module-level in-memory cache for resolved dispatch parameters (0ms overhead across same process)
_DISPATCH_CACHE: Optional[Dict[str, Any]] = None
_DISPATCH_CACHE_TIME: float = 0.0
_DISPATCH_CACHE_TTL: float = 10.0


def resolve_battery_dispatch_parameters(
    max_soc: Optional[int] = None,
    min_soc: Optional[int] = None,
    shutdown_soc: Optional[int] = None,
    current_soc: Optional[int] = None,
    loss_buffer_pct: Optional[float] = None,
    inv_cfg: Optional[Dict[str, Any]] = None,
    inv_tlm: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, int, int, float]:
    """
    Dynamically resolves SOC dispatch limits and reserve margins with zero-network cache-first policy.
    Never initiates network requests to the Deye inverter. Reuses existing local JSON cache
    (/config/.deye_inverter_cache.json) maintained by the central polling sensor.

    Priority:
    1. Explicit arguments (from CLI / HA sliders)
    2. In-Memory process cache (if recent within 10s)
    3. Inverter config/telemetry dictionary (if provided)
    4. Cached inverter readings (from solarman_v5_client cache, ignore_ttl=True)
    5. Fallback defaults from .env / energy_defaults ONLY if completely unconfigured anywhere

    Returns:
    (safe_max_soc, safe_min_soc, safe_shutdown_soc, safe_current_soc,
     eff_loss_buffer_pct)
    """
    global _DISPATCH_CACHE, _DISPATCH_CACHE_TIME
    now = time.time()

    cfg = dict(inv_cfg) if inv_cfg else {}
    tlm = dict(inv_tlm) if inv_tlm else {}

    if not cfg:
        if _DISPATCH_CACHE is not None and (now - _DISPATCH_CACHE_TIME) < _DISPATCH_CACHE_TTL:
            cfg = _DISPATCH_CACHE.get("config", {}) or {}
            tlm = _DISPATCH_CACHE.get("telemetry", {}) or {}
        else:
            try:
                import solarman_v5_client
                cached = solarman_v5_client.load_deye_cache(ignore_ttl=True)
                if cached and isinstance(cached, dict):
                    cfg = cached.get("config", {}) or {}
                    tlm = cached.get("telemetry", {}) or {}
                    _DISPATCH_CACHE = {"config": cfg, "telemetry": tlm}
                    _DISPATCH_CACHE_TIME = now
            except Exception:
                pass

    # 1. Resolve Min SOC (Reg 110: Min Discharge SOC)
    eff_min = min_soc
    if eff_min is None and cfg.get("min_discharge_soc") is not None and int(cfg["min_discharge_soc"]) > 0:
        eff_min = int(cfg["min_discharge_soc"])
    if eff_min is None:
        eff_min = energy_defaults.DEFAULT_MIN_SOC
    safe_min_soc = max(10, min(50, int(eff_min)))

    # 2. Resolve Max SOC (Reg 112: Max Charge SOC)
    eff_max = max_soc
    if eff_max is None and cfg.get("max_charge_soc") is not None and int(cfg["max_charge_soc"]) > 0:
        eff_max = int(cfg["max_charge_soc"])
    if eff_max is None:
        eff_max = energy_defaults.DEFAULT_MAX_SOC
    safe_max_soc = max(50, min(100, int(eff_max)))

    # 3. Resolve Shutdown SOC (Reg 111: Shutdown SOC)
    eff_shut = shutdown_soc
    if eff_shut is None and cfg.get("shutdown_soc") is not None and int(cfg["shutdown_soc"]) >= 0:
        eff_shut = int(cfg["shutdown_soc"])
    if eff_shut is None:
        eff_shut = energy_defaults.DEFAULT_SHUTDOWN_SOC
    safe_shutdown_soc = max(0, min(15, int(eff_shut)))

    # 4. Resolve Current SOC (Reg 588: Battery SOC)
    eff_curr = current_soc
    if eff_curr is None and tlm.get("battery_soc") is not None and int(tlm["battery_soc"]) > 0:
        eff_curr = int(tlm["battery_soc"])
    if eff_curr is None:
        eff_curr = energy_defaults.DEFAULT_CURRENT_SOC
    safe_current_soc = max(safe_min_soc, min(safe_max_soc, int(eff_curr)))

    # 5. Margins
    eff_loss = float(loss_buffer_pct) if loss_buffer_pct is not None else energy_defaults.DEFAULT_LOSS_BUFFER_PCT

    return (
        safe_max_soc,
        safe_min_soc,
        safe_shutdown_soc,
        safe_current_soc,
        eff_loss,
    )


class BatteryModel:
    """Mathematical and physical model of battery pack with configurable SOC bounds."""
    def __init__(
        self,
        capacity_kwh: Optional[float] = None,
        max_soc: Optional[float] = None,
        min_soc: Optional[float] = None,
        shutdown_soc: Optional[float] = None,
        charge_current_a: Optional[float] = None,
        discharge_current_a: Optional[float] = None,
        voltage_v: Optional[float] = None,
    ):
        def_cap, def_v, def_c, def_dc = resolve_battery_hardware_parameters(
            battery_capacity_kwh=capacity_kwh,
            nominal_voltage=voltage_v,
            max_charge_current=charge_current_a,
            max_discharge_current=discharge_current_a,
        )
        eff_max = max_soc if max_soc is not None else energy_defaults.DEFAULT_MAX_SOC
        eff_min = min_soc if min_soc is not None else energy_defaults.DEFAULT_MIN_SOC
        eff_shut = shutdown_soc if shutdown_soc is not None else energy_defaults.DEFAULT_SHUTDOWN_SOC

        self.capacity_kwh = float(capacity_kwh if capacity_kwh is not None else def_cap)
        self.max_soc = max(50.0, min(100.0, float(eff_max)))
        self.min_soc = max(10.0, min(50.0, float(eff_min)))
        self.shutdown_soc = max(0.0, min(15.0, float(eff_shut)))
        self.charge_current_a = float(charge_current_a if charge_current_a is not None else def_c)
        self.discharge_current_a = float(discharge_current_a if discharge_current_a is not None else def_dc)
        self.voltage_v = float(voltage_v if voltage_v is not None else def_v)


    @property
    def operating_capacity_kwh(self) -> float:
        return round((self.max_soc - self.min_soc) / 100.0 * self.capacity_kwh, 2)

    @property
    def blackout_reserve_kwh(self) -> float:
        return round((self.min_soc - self.shutdown_soc) / 100.0 * self.capacity_kwh, 2)

    @property
    def top_buffer_kwh(self) -> float:
        return round((100.0 - self.max_soc) / 100.0 * self.capacity_kwh, 2)

    @property
    def charge_power_kw(self) -> float:
        return round((self.voltage_v * self.charge_current_a) / 1000.0, 2)

    @property
    def discharge_power_kw(self) -> float:
        return round((self.voltage_v * self.discharge_current_a) / 1000.0, 2)

    @property
    def hours_to_full_charge(self) -> float:
        if self.charge_power_kw <= 0:
            return 0.0
        return round(self.operating_capacity_kwh / self.charge_power_kw, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capacity_kwh": self.capacity_kwh,
            "max_soc": self.max_soc,
            "min_soc": self.min_soc,
            "shutdown_soc": self.shutdown_soc,
            "operating_capacity_kwh": self.operating_capacity_kwh,
            "blackout_reserve_kwh": self.blackout_reserve_kwh,
            "top_buffer_kwh": self.top_buffer_kwh,
            "charge_power_kw": self.charge_power_kw,
            "discharge_power_kw": self.discharge_power_kw,
            "hours_to_full_charge": self.hours_to_full_charge,
        }


def find_ev_best_window(
    data: Dict[str, Any],
    target_hours: Any = "auto",
    now_dt: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Finds the optimal contiguous window for Electric Vehicle charging.
    Defaults: Weekdays (Mon-Fri) = 2h, Weekends (Sat-Sun) = 4h (or configured).
    """
    if not data or not isinstance(data, dict) or "error" in data:
        return {"error": data.get("error", "No data") if isinstance(data, dict) else "No data"}

    frames = data.get("frames", [])
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
        return {"error": "No valid pricing frames"}

    ref_dt = now_dt or datetime.now(WARSAW_TZ)
    if target_hours == "auto" or target_hours is None:
        first_start = valid_frames[0].get("start")
        if first_start:
            try:
                frame_dt = datetime.fromisoformat(first_start.replace("Z", "+00:00")).astimezone(WARSAW_TZ)
                wday = frame_dt.weekday()
            except Exception:
                wday = ref_dt.weekday()
        else:
            wday = ref_dt.weekday()
        n_hours = 4 if wday >= 5 else 2
    else:
        try:
            n_hours = int(str(target_hours).replace("h", "").strip())
        except ValueError:
            n_hours = 2

    n_hours = max(1, min(len(valid_frames), n_hours))
    search_frames = valid_frames[:24] if len(valid_frames) >= 24 else valid_frames
    if len(search_frames) < n_hours:
        search_frames = valid_frames

    def get_gross(f):
        p = f["metrics"]["pricing"]
        if p.get("price_gross") is not None:
            return float(p["price_gross"])
        if p.get("full_price") is not None:
            return float(p["full_price"])
        if p.get("price_net") is not None:
            return round(float(p["price_net"]) * 1.23, 4)
        return 999.0

    best_idx = 0
    best_avg = float("inf")
    for i in range(len(search_frames) - n_hours + 1):
        window = search_frames[i : i + n_hours]
        avg = sum(get_gross(f) for f in window) / n_hours
        if avg < best_avg:
            best_avg = avg
            best_idx = i

    chosen = search_frames[best_idx : best_idx + n_hours]
    gross_prices = [get_gross(f) for f in chosen]
    avg_price = round(sum(gross_prices) / len(gross_prices), 3)
    start_str = chosen[0].get("start")
    end_str = chosen[-1].get("end")
    start_fmt = format_hhmm(start_str)
    end_fmt = format_hhmm(end_str)

    return {
        "start": start_str,
        "end": end_str,
        "duration_hours": n_hours,
        "average_price": avg_price,
        "min_price": round(min(gross_prices), 3),
        "max_price": round(max(gross_prices), 3),
        "display": f"{start_fmt} - {end_fmt} (śr. {avg_price:.2f} zł/kWh)",
    }


def retail_gross_price(f: Dict[str, Any]) -> float:
    """Gross retail buy price of one pricing frame (price_gross, full_price, or price_net * VAT)."""
    p = (f.get("metrics") or {}).get("pricing") or {}
    if p.get("price_gross") is not None:
        return float(p["price_gross"])
    if p.get("full_price") is not None:
        return float(p["full_price"])
    if p.get("price_net") is not None:
        return round(float(p["price_net"]) * energy_defaults.VAT_MULTIPLIER, 4)
    return energy_defaults.PRICE_MISSING_SENTINEL


def priced_day_frames(frames: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Frames that carry a start time and a gross/net price, limited to one day (first 24 h)."""
    valid = [
        f for f in frames or []
        if isinstance(f, dict)
        and f.get("start")
        and (
            ((f.get("metrics") or {}).get("pricing") or {}).get("price_gross") is not None
            or ((f.get("metrics") or {}).get("pricing") or {}).get("price_net") is not None
        )
    ]
    return valid[:24]


def hhmm_to_minutes(hhmm: Optional[str]) -> Optional[int]:
    """'HH:MM' -> minutes since midnight (None if unparsable, e.g. '--:--')."""
    try:
        hh, mm = str(hhmm).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, TypeError):
        return None


def charge_energy_min_hours(
    current_soc: float, max_soc: float, capacity_kwh: float, voltage: float, ceiling_a: float, loss_pct: float
) -> int:
    """
    Shortest whole-hour grid charge at the Reg 108 ceiling covering the energy need:
    need_kwh = (max_soc - current_soc)/100 * capacity_kwh * (1 + loss/100);  min_h = ceil(need_kwh / (ceiling_a * V / 1000)).
    """
    need_kwh = max(0.0, float(max_soc) - float(current_soc)) / 100.0 * float(capacity_kwh) * (1.0 + float(loss_pct) / 100.0)
    power_kw = float(ceiling_a) * float(voltage) / 1000.0
    return max(1, math.ceil(need_kwh / power_kw)) if power_kw > 0 else 1


def charge_window_min_hours(
    current_soc: float, max_soc: float, capacity_kwh: float, voltage: float, ceiling_a: float, loss_pct: float
) -> int:
    """N = max(energy-need hours at the Reg 108 ceiling, CHARGE_WINDOW_MIN_HOURS user minimum)."""
    return max(
        int(energy_defaults.CHARGE_WINDOW_MIN_HOURS),
        charge_energy_min_hours(current_soc, max_soc, capacity_kwh, voltage, ceiling_a, loss_pct),
    )


def charge_current_for_window(min_hours: int, window_hours: int, ceiling_a: Optional[float] = None) -> int:
    """Reg 108 target inside a charge window of T hours: clamp(round(ceiling * min_h / T), floor, ceiling)."""
    ceiling = float(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A if ceiling_a is None else ceiling_a)
    scaled = round(ceiling * float(min_hours) / float(max(1, window_hours)))
    return int(max(energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A, min(ceiling, scaled)))


def analyse_day_price_profile(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Splits one Warsaw day of hourly prices at the day-mean gross price (the only, parameter-free threshold):
    contiguous hours below the mean form "valley" segments, hours at or above the mean form "peak" segments.
    Each segment: kind, start_m/end_m (Warsaw minutes since midnight), avg/min/max gross price, hours (count),
    start/end (ISO of its first/last frame) and start_index/end_index into priced_day_frames(frames).
    "peaks" ranks the peak segments by average gross retail buy price (Rule 12: never the prosumer sell price).
    """
    priced = priced_day_frames(frames)
    if not priced:
        return {"mean": 0.0, "segments": [], "peaks": []}
    prices = [retail_gross_price(f) for f in priced]
    total = math.fsum(prices)
    n = len(prices)
    segments: List[Dict[str, Any]] = []
    for i, (f, price) in enumerate(zip(priced, prices)):
        kind = "peak" if price * n >= total else "valley"  # price >= mean, without float division error
        start_m = hhmm_to_minutes(format_hhmm(f.get("start")))
        if start_m is None:
            continue
        if not segments or segments[-1]["kind"] != kind:
            segments.append({"kind": kind, "start_m": start_m, "start": f.get("start"), "start_index": i, "_prices": []})
        seg = segments[-1]
        seg["_prices"].append(price)
        seg["end_m"] = start_m + 60
        seg["end"] = f.get("end")
        seg["end_index"] = i + 1
    for seg in segments:
        ps = seg.pop("_prices")
        seg.update(hours=len(ps), avg=round(math.fsum(ps) / len(ps), 4), min=min(ps), max=max(ps))
    peaks = sorted((s for s in segments if s["kind"] == "peak"), key=lambda s: -s["avg"])
    return {"mean": round(total / n, 4), "segments": segments, "peaks": peaks}


def find_powerbank_best_window(
    data: Dict[str, Any],
    target_hours: Any = "auto",
    allow_disjoint: bool = False,
    now_dt: Optional[datetime] = None,
    *,
    current_soc: Optional[float] = None,
    max_soc: Optional[float] = None,
    capacity_kwh: Optional[float] = None,
    voltage: Optional[float] = None,
    ceiling_a: Optional[float] = None,
    loss_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Finds the home battery (Power Bank) grid-charge window.
    auto: derived only from the day's prices and the energy need (no clock, weekday or tolerance heuristics):
      - N = charge_window_min_hours(...): max(energy need at the Reg 108 ceiling, CHARGE_WINDOW_MIN_HOURS),
        capped at the day length;
      - the cheapest contiguous N-hour block inside a below-mean price valley (cheapest block of the day if no valley fits);
      - stretched hour by hour (cheaper neighbour first) only with hours of the same valley whose gross price is
        <= that valley's mean gross price, while clamp(round(ceiling * N / T)) stays >= BATTERY_CHARGE_CURRENT_MIN_FLOOR_A.
    Explicit target_hours (user config): the cheapest contiguous block (or, with allow_disjoint, the cheapest hours).
    SOC/capacity/voltage/loss default to the cache-first dispatch/hardware resolution; ceiling to the Reg 108 ceiling.
    """
    if not data or not isinstance(data, dict) or "error" in data:
        return {"error": data.get("error", "No data") if isinstance(data, dict) else "No data"}

    search_frames = priced_day_frames(data.get("frames", []))
    if not search_frames:
        return {"error": "No valid pricing frames"}
    get_gross = retail_gross_price
    min_hours: Optional[int] = None

    if target_hours == "auto" or target_hours is None:
        if current_soc is None or max_soc is None or loss_pct is None:
            r_max, _, _, r_cur, _, r_loss = resolve_battery_dispatch_parameters(
                max_soc=max_soc, current_soc=current_soc, loss_buffer_pct=loss_pct,
            )
            current_soc = r_cur if current_soc is None else current_soc
            max_soc = r_max if max_soc is None else max_soc
            loss_pct = r_loss if loss_pct is None else loss_pct
        cap, volts, _, _ = resolve_battery_hardware_parameters(battery_capacity_kwh=capacity_kwh, nominal_voltage=voltage)
        ceiling = energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A
        if ceiling_a is not None and float(ceiling_a) > 0:
            ceiling = min(ceiling, float(ceiling_a))
        min_hours = charge_window_min_hours(current_soc, max_soc, cap, volts, ceiling, loss_pct)

        prices = [get_gross(f) for f in search_frames]
        segments = analyse_day_price_profile(search_frames)["segments"]
        n_h = min(min_hours, len(prices))

        def blocks(lo: int, hi: int) -> List[Tuple[float, int]]:
            return [(math.fsum(prices[i : i + n_h]) / n_h, i) for i in range(lo, hi - n_h + 1)]

        candidates = [
            (avg, i, seg) for seg in segments if seg["kind"] == "valley"
            for avg, i in blocks(seg["start_index"], seg["end_index"])
        ]
        if candidates:
            _, start_idx, valley = min(candidates, key=lambda c: (c[0], c[1]))
        else:
            valley = None
            _, start_idx = min(blocks(0, len(prices)))
        end_idx = start_idx + n_h

        if valley is not None:
            def eligible(i: int) -> bool:
                in_valley = valley["start_index"] <= i < valley["end_index"]
                unclamped_a = round(float(ceiling) * min_hours / (end_idx - start_idx + 1))
                return in_valley and prices[i] <= valley["avg"] and unclamped_a >= energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A

            while True:
                left = start_idx - 1 if eligible(start_idx - 1) else None
                right = end_idx if eligible(end_idx) else None
                if left is None and right is None:
                    break
                if right is not None and (left is None or prices[right] <= prices[left]):
                    end_idx += 1
                else:
                    start_idx -= 1

        chosen_frames = search_frames[start_idx:end_idx]
        is_consecutive = True
    else:
        try:
            n_hours = int(str(target_hours).replace("h", "").strip())
        except ValueError:
            n_hours = 2

        n_hours = max(1, min(len(search_frames), n_hours))

        if allow_disjoint:
            indexed_frames = list(enumerate(search_frames))
            indexed_frames.sort(key=lambda item: get_gross(item[1]))
            chosen_indexed = indexed_frames[:n_hours]
            chosen_indexed.sort(key=lambda item: item[0])
            chosen_frames = [item[1] for item in chosen_indexed]
            chosen_indices = [item[0] for item in chosen_indexed]

            is_consecutive = all(
                chosen_indices[i + 1] == chosen_indices[i] + 1
                for i in range(len(chosen_indices) - 1)
            )
        else:
            best_idx = 0
            best_avg = float("inf")
            for i in range(len(search_frames) - n_hours + 1):
                window = search_frames[i : i + n_hours]
                avg = sum(get_gross(f) for f in window) / n_hours
                if avg < best_avg:
                    best_avg = avg
                    best_idx = i
            chosen_frames = search_frames[best_idx : best_idx + n_hours]
            is_consecutive = True

    gross_prices = [get_gross(f) for f in chosen_frames]
    avg_price = round(sum(gross_prices) / len(gross_prices), 3)

    slots = [f"{format_hhmm(f.get('start'))} - {format_hhmm(f.get('end'))}" for f in chosen_frames]
    start_str = chosen_frames[0].get("start")
    end_str = chosen_frames[-1].get("end")

    if is_consecutive:
        display = f"{format_hhmm(start_str)} - {format_hhmm(end_str)} (śr. {avg_price:.2f} zł/kWh)"
    else:
        slots_str = " oraz ".join(slots)
        display = f"{slots_str} (śr. {avg_price:.2f} zł/kWh)"

    return {
        "start": start_str,
        "end": end_str,
        "duration_hours": len(chosen_frames),
        "min_hours": min_hours,
        "average_price": avg_price,
        "min_price": round(min(gross_prices), 3),
        "max_price": round(max(gross_prices), 3),
        "slots": slots,
        "is_consecutive": is_consecutive,
        "display": display,
    }


def find_best_sell_window(
    data: Dict[str, Any],
    target_hours: int = 3
) -> Dict[str, Any]:
    """
    Finds the optimal contiguous peak window to sell energy or discharge home battery into grid,
    plus identifies the highest 1-hour spike.
    """
    if not data or not isinstance(data, dict) or "error" in data:
        return {"error": data.get("error", "No data") if isinstance(data, dict) else "No data"}

    frames = data.get("frames", [])
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
        return {"error": "No valid pricing frames"}

    search_frames = valid_frames[:24] if len(valid_frames) >= 24 else valid_frames

    def get_gross_sell(f):
        p = f["metrics"]["pricing"]
        if p.get("price_prosumer_gross") is not None:
            return float(p["price_prosumer_gross"])
        if p.get("sell_price_gross") is not None:
            return float(p["sell_price_gross"])
        if p.get("price_prosumer_net") is not None:
            return round(float(p["price_prosumer_net"]) * 1.23, 4)
        if p.get("sell_price_net") is not None:
            return round(float(p["sell_price_net"]) * 1.23, 4)
        if p.get("price_gross") is not None:
            return float(p["price_gross"])
        if p.get("price_net") is not None:
            return round(float(p["price_net"]) * 1.23, 4)
        return 0.0

    n_hours = max(1, min(len(search_frames), target_hours))
    best_idx = 0
    best_avg = -1.0

    for i in range(len(search_frames) - n_hours + 1):
        window = search_frames[i : i + n_hours]
        avg = sum(get_gross_sell(f) for f in window) / n_hours
        if avg > best_avg:
            best_avg = avg
            best_idx = i

    chosen = search_frames[best_idx : best_idx + n_hours]
    sell_prices = [get_gross_sell(f) for f in chosen]
    avg_price = round(sum(sell_prices) / len(sell_prices), 3)

    spike_f = max(chosen, key=get_gross_sell)
    spike_price = round(get_gross_sell(spike_f), 3)
    spike_hour = f"{format_hhmm(spike_f.get('start'))} - {format_hhmm(spike_f.get('end'))}"

    start_str = chosen[0].get("start")
    end_str = chosen[-1].get("end")
    start_fmt = format_hhmm(start_str)
    end_fmt = format_hhmm(end_str)

    display = f"{start_fmt} - {end_fmt} (śr. {avg_price:.2f} zł/kWh, pik {format_hhmm(spike_f.get('start'))}: {spike_price:.2f} zł)"

    return {
        "start": start_str,
        "end": end_str,
        "duration_hours": n_hours,
        "average_price": avg_price,
        "min_price": round(min(sell_prices), 3),
        "max_price": round(max(sell_prices), 3),
        "spike_hour": spike_hour,
        "spike_price": spike_price,
        "display": display,
    }


def find_best_discharge_windows(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Battery discharge peak windows = the above-day-mean "peak" segments of analyse_day_price_profile, ranked by
    gross retail buy price (price_gross, NOT the prosumer sell price: zero-export self-consumption, Rule 12).
    Returns the highest-ranked peak's window fields at top level plus "peaks" (every peak segment, time ordered,
    each with its "rank", 1 = most expensive) and "is_dual_peak".
    """
    if not data or not isinstance(data, dict) or "error" in data:
        return {"error": data.get("error", "No data") if isinstance(data, dict) else "No data"}

    priced = priced_day_frames(data.get("frames", []))
    if not priced:
        return {"error": "No valid pricing frames"}
    profile = analyse_day_price_profile(priced)

    def _window(seg: Dict[str, Any], rank: int) -> Dict[str, Any]:
        w_frames = priced[seg["start_index"] : seg["end_index"]]
        spike_f = max(w_frames, key=retail_gross_price)
        spike_price = round(retail_gross_price(spike_f), 3)
        start_fmt = format_hhmm(seg["start"])
        end_fmt = format_hhmm(w_frames[-1].get("end")) if w_frames[-1].get("end") else "--:--"
        avg_p = seg["avg"]
        return {
            "start": seg["start"],
            "end": w_frames[-1].get("end"),
            "start_index": seg["start_index"],
            "end_index": seg["end_index"],
            "start_time": start_fmt,
            "end_time": end_fmt,
            "duration_hours": seg["hours"],
            "average_price": round(avg_p, 3),
            "min_price": round(seg["min"], 3),
            "max_price": round(seg["max"], 3),
            "rank": rank,
            "spike_hour": f"{format_hhmm(spike_f.get('start'))} - {format_hhmm(spike_f.get('end'))}",
            "spike_price": spike_price,
            "display": f"{start_fmt} - {end_fmt} (śr. {avg_p:.2f} zł/kWh, pik {format_hhmm(spike_f.get('start'))}: {spike_price:.2f} zł)",
        }

    ranked = [_window(seg, rank) for rank, seg in enumerate(profile["peaks"], start=1)]
    peaks = sorted(ranked, key=lambda p: p["start_index"])
    result = dict(ranked[0])
    result["peaks"] = peaks
    result["is_dual_peak"] = len(peaks) > 1
    return result


def _iso_dt(iso: Any) -> Optional[datetime]:
    if isinstance(iso, datetime):
        return iso
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")




def _is_weekend(day: Any) -> bool:
    """Saturday/Sunday: selects the weekend half of the household load profile (measured load, not a dispatch rule)."""
    return day.weekday() >= 5


def _profile_kind(local_dt: datetime) -> str:
    return "weekend" if _is_weekend(local_dt.date()) else "weekday"


def build_load_profile(history: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, List[float]]]:
    """
    Hour-of-day household load profile (kWh/h = mean W / 1000) from Deye station history frames
    {ts (aware or ISO), consumption_w}: Europe/Warsaw local hour buckets (DST-correct), weekdays and weekends kept
    apart. An hour without samples takes the overall mean of the whole history. None without usable history.
    """
    acc: Dict[str, List[List[float]]] = {k: [[0.0, 0.0] for _ in range(24)] for k in ("weekday", "weekend")}
    total, count = 0.0, 0.0
    for f in history or []:
        ts = _iso_dt(f.get("ts")) if isinstance(f, dict) else None
        try:
            w = float(f.get("consumption_w"))  # type: ignore[union-attr]
        except (TypeError, ValueError, AttributeError):
            continue
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(WARSAW_TZ)
        cell = acc[_profile_kind(local)][local.hour]
        cell[0] += w
        cell[1] += 1.0
        total += w
        count += 1.0
    if count <= 0:
        return None
    mean_kwh = total / count / 1000.0
    return {k: [round(s / n / 1000.0 if n else mean_kwh, 4) for s, n in v] for k, v in acc.items()}


def load_forecast(
    start: datetime, end: datetime, profile: Optional[Dict[str, List[float]]], live_kw: Optional[float] = None
) -> List[Dict[str, Any]]:
    """
    Per-hour household load forecast [{start, kwh}] over [start, end) from the hourly profile (Warsaw local hour,
    weekday/weekend by Warsaw date). The first (running) hour may be blended with the live load (kW, snapped to
    LOAD_ESTIMATE_STEP_KW). Partial hours are prorated. [] without a profile.
    """
    out: List[Dict[str, Any]] = []
    if not profile or end <= start:
        return out
    live = snap_load_kw(live_kw)
    t = start
    while t < end:
        hour_start = t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        nxt = min(end, hour_start + timedelta(hours=1))
        local = t.astimezone(WARSAW_TZ)
        rate = float(profile[_profile_kind(local)][local.hour])
        if live is not None and not out:
            rate = (rate + live) / 2.0
        out.append({"start": t, "kwh": rate * (nxt - t).total_seconds() / 3600.0})
        t = nxt
    return out


def _soc_floor(soc: float) -> int:
    """Whole-percent SOC floor, robust to float noise (54.9999999 -> 55)."""
    return int(math.floor(soc + 1e-6))


def _soc_ceil(soc: float) -> int:
    """Whole-percent SOC ceiling, robust to float noise (29.0000001 -> 29)."""
    return int(math.ceil(soc - 1e-6))


def snap_load_kw(load_kw: Optional[float]) -> Optional[float]:
    """Household load snapped to LOAD_ESTIMATE_STEP_KW (hysteresis: small load jitter never changes the plan)."""
    if load_kw is None:
        return None
    step = float(energy_defaults.LOAD_ESTIMATE_STEP_KW)
    return round(round(max(0.0, float(load_kw)) / step) * step, 3) if step > 0 else float(load_kw)


def _snap_floor(soc: float, min_soc: float, max_soc: float, up: bool = False) -> int:
    """A computed non-charge floor snapped to TOU_FLOOR_SOC_STEP inside [Min SOC, Max SOC], so hourly re-syncs with a
    drifting SOC / load write the same table (flash wear). Down for a projected-SOC hold (never above the projected
    level), up for an energy need (a reserve is never short of what the peak needs)."""
    step = max(1, int(energy_defaults.TOU_FLOOR_SOC_STEP))
    v = (-(-_soc_ceil(soc) // step) if up else _soc_floor(soc) // step) * step
    return int(min(max(v, int(min_soc)), int(max_soc)))


def project_soc_timeline(
    slots: List[Dict[str, Any]],
    start_soc: float,
    load_kw: Optional[float],
    now_dt: datetime,
    *,
    min_soc: float,
    max_soc: float,
    capacity_kwh: float,
    charge_kw: float,
) -> List[Dict[str, Any]]:
    """
    Pure SOC projection over time-ordered slots [{start, end (aware), target_soc, grid_charge[, load_kw]}] from now_dt
    with Deye TOU semantics: a non-charge slot discharges for its load (the slot's own "load_kw" = the hourly profile,
    else load_kw) down to its target_soc floor and, at or below that floor, stays (the grid covers the load, no grid
    charge); a charge slot grid-charges at charge_kw (kW into the battery, loss already applied) up to target_soc
    (never above max_soc) while the grid covers the load, and discharges for the load down to it when above.
    Slots ending before now_dt are dropped, a running one starts at now_dt.
    Returns [{start, end, soc_start, soc_end}] (SOC in %).
    """
    soc, cap = float(start_soc), float(capacity_kwh)
    out: List[Dict[str, Any]] = []
    for sl in slots:
        a, b = max(sl["start"], now_dt), sl["end"]
        if b <= a:
            continue
        load = max(0.0, float(sl.get("load_kw", load_kw) or 0.0))
        hrs = (b - a).total_seconds() / 3600.0
        floor, start = float(sl["target_soc"]), soc
        if sl.get("grid_charge") and soc < floor:
            rate = float(sl.get("charge_kw", charge_kw))
            soc = min(soc + rate * hrs * 100.0 / cap, floor, float(max_soc)) if cap > 0 else soc
        elif soc > floor:
            soc = max(floor, soc - load * hrs * 100.0 / cap) if cap > 0 else soc
        out.append({"start": a, "end": b, "soc_start": start, "soc_end": soc})
    return out


def _cell_hours(c: Dict[str, Any]) -> float:
    return (c["end"] - c["start"]).total_seconds() / 3600.0


def _assign_cell_floors(cells: List[Dict[str, Any]], *, min_soc: float, top: float, capacity_kwh: float,
                        loss_pct: float, has_load: bool) -> None:
    """
    Floors (target SOC) and roles of the horizon cells, all data-driven:
      - main charge -> grid charge to Max SOC (100 % in calibration); top-up -> grid charge to its own target;
      - peak -> Min SOC, or a reserve when a later peak before the next main charge costs more than this peak *
        (1 + loss%): Min SOC + that later peak's forecast kWh / capacity (snapped up), capped at Max SOC;
      - valley -> next event (top-ups skipped) a main charge: drain to Min SOC; a peak: keep what that peak needs
        (its own floor + its forecast kWh / capacity, snapped up), and with a top-up in the same valley run at least the
        top-up target (the grid covers the cheap valley's load up to that peak) - without a load profile hold at Max SOC (the battery
        idles in the cheap valley and serves the peak); no event left in the known prices: hold after a charge, else
        drain to Min SOC.
    role: charge | topup | peak | hold (floor above Min SOC) | drain.
    """
    cap = float(capacity_kwh)
    pk_kwh: Dict[Any, float] = {}
    pk_avg: Dict[Any, float] = {}
    pk_last: Dict[Any, int] = {}
    for i, c in enumerate(cells):
        if c["kind"] == "peak":
            pk_kwh[c["peak"]] = pk_kwh.get(c["peak"], 0.0) + c["load_kw"] * _cell_hours(c)
            pk_avg[c["peak"]] = c["seg_avg"]
            pk_last[c["peak"]] = i
    pk_floor: Dict[Any, int] = {}
    for pid, last in pk_last.items():
        later = 0.0
        for c in cells[last + 1:]:
            if c["kind"] == "charge":
                break
            if c["kind"] == "peak" and pk_avg[c["peak"]] > pk_avg[pid] * (1.0 + float(loss_pct) / 100.0):
                later += c["load_kw"] * _cell_hours(c)
        pk_floor[pid] = (_snap_floor(float(min_soc) + later * 100.0 / cap, min_soc, top, up=True)
                         if has_load and later > 0 and cap > 0 else int(min_soc))
    for i, c in enumerate(cells):
        if c["kind"] == "charge":
            c.update(role="charge", target_soc=int(top), grid_charge=True, charge_a=c["charge"]["charge_a"])
            continue
        if c["kind"] == "topup":
            c.update(role="topup", grid_charge=True)
            continue
        c.update(grid_charge=False, charge_a=0)
        if c["kind"] == "peak":
            c.update(role="peak", target_soc=pk_floor[c["peak"]])
            continue
        nxt = next((x for x in cells[i + 1:] if x["kind"] in ("peak", "charge")), None)
        if nxt is None:
            prv = next((x for x in reversed(cells[:i]) if x["kind"] != "valley"), None)
            target = int(top) if prv is not None and prv["kind"] in ("charge", "topup") else int(min_soc)
        elif nxt["kind"] == "charge":
            target = int(min_soc)
        elif has_load and cap > 0:
            target = _snap_floor(pk_floor[nxt["peak"]] + pk_kwh[nxt["peak"]] * 100.0 / cap, min_soc, top, up=True)
            # the cheap valley keeps what the load from that peak to the next main charge needs (what a top-up there
            # would buy: no drain now + re-charge later), and with a top-up in this run at least its target
            hi = cells.index(nxt)
            cyc = math.fsum(x["load_kw"] * _cell_hours(x) for x in
                            itertools.takewhile(lambda x: x["kind"] != "charge", cells[hi:]))
            lo = next((j + 1 for j in range(i - 1, -1, -1) if cells[j]["kind"] not in ("valley", "topup")), 0)
            target = max([target, _snap_floor(float(min_soc) + cyc * 100.0 / cap, min_soc, top, up=True)]
                         + [int(x["target_soc"]) for x in cells[lo:hi] if x["kind"] == "topup"])
        else:
            target = int(top)
        c.update(role="hold" if target > int(min_soc) else "drain", target_soc=target)


def _merge_timeline(cells: List[Dict[str, Any]], proj: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Consecutive cells with the same behaviour (role, target SOC, charge current) as one timeline entry; estimated
    cells are never merged."""
    out: List[Dict[str, Any]] = []
    for c, p in zip(cells, proj):
        # estimated hours stay hourly entries (each shows its own estimated price)
        key = (c["role"], c["target_soc"], c["charge_a"], c["start"] if c.get("estimated") else None)
        if out and out[-1]["_key"] == key:
            e = out[-1]
            e["_w"].append((_cell_hours(c), c["price"]))
            e.update(end=c["end"], soc_end=p["soc_end"])
        else:
            out.append({"_key": key, "start": c["start"], "end": c["end"], "role": c["role"],
                        "target_soc": int(c["target_soc"]), "grid_charge": c["grid_charge"], "charge_a": int(c["charge_a"]),
                        "estimated": bool(c.get("estimated")), "soc_start": p["soc_start"], "soc_end": p["soc_end"], "_w": [(_cell_hours(c), c["price"])]})
    for e in out:
        w = e.pop("_w")
        e.pop("_key")
        tot = math.fsum(x for x, _ in w)
        e["price"] = round(math.fsum(x * p for x, p in w) / tot, 4) if tot > 0 else w[0][1]
    return out


def _fit_tou_table(entries: List[Dict[str, Any]], h0: datetime, slot_count: int,
                   rows: Optional[List[Tuple[int, bool, int, int]]] = None) -> List[Dict[str, Any]]:
    """
    The written table covers [h0, h0 + 24 h) and repeats daily: the timeline clipped to it (the last entry stretched
    when the known prices end earlier), then fitted to exactly slot_count slots on the daily circle: identical
    neighbours merge first, then non-charge neighbours with the smallest price difference (a peak start kept), then a
    charge runs on into a following hold slot (at its target it only holds), and only as a last resort a peak is
    absorbed by the slot before it or a charge runs on into a slot where the battery serves the load; with fewer
    slots the longest non-charge slot splits on an hour boundary.
    rows (the inverter's table, _tou_minute_rows): within a tier, a merge the inverter already runs (the absorbed slot
    has the kept slot's behaviour there) goes first, so re-syncs pick the same merges (flash wear).
    Returns the segments sorted by Warsaw clock minute.
    """
    t_end = h0 + timedelta(hours=24)
    segs = []
    for e in entries:
        a, b = max(e["start"], h0), min(e["end"], t_end)
        if not segs:
            a = h0
        if b > a:
            segs.append(dict(e, start=a, end=b))
    if not segs:
        return []
    segs[-1]["end"] = t_end
    beh = lambda s: (s["grid_charge"], s["target_soc"], s["charge_a"])
    while len(segs) > slot_count:
        n = len(segs)
        cands = []
        for i in range(n):
            p, c = segs[i], segs[(i + 1) % n]
            peak_start = c["role"] == "peak" and p["role"] != "peak"
            if beh(p) == beh(c) and not peak_start:
                tier = 0
            elif not p["grid_charge"] and not c["grid_charge"]:
                tier = 3 if peak_start else 1
            elif p["grid_charge"] and not c["grid_charge"]:
                # a charge only runs on into a hold (at its target it only holds); into a slot where the battery
                # serves the load (drain/peak) it would grid-charge through it, so only when forced
                tier = 2 if c["role"] == "hold" else 4
            elif p["grid_charge"] and c["grid_charge"]:
                tier = 5  # too many separate charge blocks: merge the closest-priced pair (the higher current kept)
            else:
                continue
            cost = 0.0 if tier == 0 else abs(p["price"] - c["price"])
            known = 1
            if rows:
                loc = c["start"].astimezone(WARSAW_TZ)
                r = _tou_row_at(rows, loc.hour * 60 + loc.minute)
                known = 0 if (r[1], r[2]) == (bool(p["grid_charge"]), int(p["target_soc"])) else 1
            # at equal cost keep the table start (its anchor) rather than merging across it
            cands.append((tier >= 4, known, tier, cost, (i + 1) % n == 0, (c["end"] - c["start"]).total_seconds(), i))
        if not cands:
            break
        i = min(cands)[-1]
        p, c = segs[i], segs[(i + 1) % n]
        wp, wc = (p["end"] - p["start"]).total_seconds(), (c["end"] - c["start"]).total_seconds()
        merged = dict(p, end=p["end"] + (c["end"] - c["start"]), charge_a=max(p["charge_a"], c["charge_a"]),
                      price=round((p["price"] * wp + c["price"] * wc) / (wp + wc), 4) if wp + wc > 0 else p["price"])
        if p["grid_charge"] and c["grid_charge"]:
            # tier 5: the merged slot runs over both at the higher current to the higher target, so every planned
            # charge still fits (coverage kept; it can only store more, bounded by that target)
            merged.update(target_soc=max(p["target_soc"], c["target_soc"]),
                          role="charge" if "charge" in (p["role"], c["role"]) else p["role"])
        if (i + 1) % n == 0:  # the wrap pair: the last slot runs on through the table start
            segs = segs[1:-1] + [merged]
        else:
            segs[i:i + 2] = [merged]
    step = timedelta(minutes=max(1, int(energy_defaults.TOU_SLOT_MINUTE_STEP)))
    while len(segs) < slot_count:
        k = min(range(len(segs)), key=lambda x: (segs[x]["grid_charge"], -(segs[x]["end"] - segs[x]["start"]), x))
        s = segs[k]
        length = s["end"] - s["start"]
        hours = int(length.total_seconds() // 3600)
        mid = timedelta(hours=statistics.median_low(range(1, hours))) if hours > 1 else (length // 2) // step * step
        if mid <= timedelta(0):
            break
        segs[k:k + 1] = [dict(s, end=s["start"] + mid), dict(s, start=s["start"] + mid)]
    for s in segs:
        local = s["start"].astimezone(WARSAW_TZ)
        s["clock_m"] = local.hour * 60 + local.minute
    return sorted(segs, key=lambda s: s["clock_m"])


def _tou_table_slots(segs: List[Dict[str, Any]], *, min_soc: float, voltage: float, discharge_a: float,
                     base_power_w: int, calibration_active: bool) -> List[Dict[str, Any]]:
    """Deye 6-slot dicts (time, power, target SOC, grid charge, Reg 108 current, label, role) of the fitted segments."""
    n = len(segs)
    to_time = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"
    span = lambda i: f"({to_time(segs[i]['clock_m'])}-{to_time(segs[(i + 1) % n]['clock_m'])})"
    events = [i for i in range(n) if segs[i]["grid_charge"] or segs[i]["role"] == "peak"]
    peaks = [i for i in range(n) if segs[i]["role"] == "peak"]
    mains = [i for i in range(n) if segs[i]["role"] == "charge"]
    main_a = segs[mains[0]]["charge_a"] if mains else int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))
    ceiling_a = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))
    power = lambda a: min(energy_defaults.INVERTER_MAX_POWER_W, max(energy_defaults.INVERTER_MIN_POWER_W, int(round(a * voltage, -1))))
    out = []
    for i, s in enumerate(segs):
        prev_ev = next((segs[j] for j in reversed(events) if j < i), segs[events[-1]] if events else None)
        next_ev = next((segs[j] for j in events if j > i), segs[events[0]] if events else None)
        head = not any(j < i for j in events)
        if s["role"] == "topup":
            label, role = f"Doładowanie przed Szczytem {span(i)}", "charge"
        elif s["role"] == "charge":
            name = "Najtańsze Ładowanie" if i == mains[0] else "Tania Godzina (Doładowanie)"
            label, role = ("Ładowanie (Kalibracja 100%)" if calibration_active else f"{name} {span(i)}"), "charge"
        elif s["role"] == "peak":
            first, last = i == peaks[0], i == peaks[-1]
            name = "Szczyt Cenowy" if first == last else ("Szczyt Poranny" if first else "Szczyt Wieczorny")
            mode = "Autokonsumpcja" if s["target_soc"] <= min_soc else "Autokonsumpcja do Rezerwy"
            label, role = f"{name} ({mode}) {span(i)}", "peak"
        elif s["role"] == "hold":
            if prev_ev is not None and prev_ev["grid_charge"] and next_ev is not None and next_ev["role"] == "peak":
                label = "Ochrona Rezerwy (Hold do Szczytu)" if head else "Czuwanie po Ładowaniu (Hold do Szczytu)"
            elif next_ev is not None and next_ev["role"] == "peak":
                label = "Ochrona Rezerwy (Hold do Szczytu)"
            else:
                label = "Ochrona Rezerwy (Hold)"
            role = "reserve"
        else:
            before_charge = prev_ev is not None and prev_ev["role"] == "peak" and next_ev is not None and next_ev["role"] == "charge"
            label = "Autokonsumpcja do Min SOC (przed Ładowaniem)" if before_charge and not head else "Czuwanie Nocne"
            role = "other"
        grid = bool(s["grid_charge"])
        a = int(s["charge_a"]) if grid else 0
        act_a = a if grid else int(round(discharge_a))
        act_w = power(a) if grid else base_power_w
        out.append({
            "slot": i + 1,
            "time": to_time(s["clock_m"]),
            "power_w": base_power_w,
            "target_soc": int(s["target_soc"]),
            "grid_charge": grid,
            "charge_current_a": a,
            "charge_power_w": power(a) if grid else 0,
            "discharge_current_a": int(round(discharge_a)),
            "discharge_power_w": base_power_w,
            "active_current_a": act_a,
            "active_power_w": act_w,
            "display_current_a": act_a,
            "display_power_w": act_w,
            "target_charge_current_a": a if grid else main_a,
            "base_charge_current_a": ceiling_a,
            "label": label,
            "role": role,
            "topup": s["role"] == "topup",  # peak top-up (not the main charge); ignored by write paths and slot diffing
            "voltage": voltage,
        })
    return out


_KWH_EPS = 1e-6  # kWh below this is float noise, not a shortfall


def _soc_walk(cand: List[Tuple[Any, ...]], worth: List[bool], charges: Dict[int, float], soc: float,
              load_kwh_by_hour: Dict[datetime, float], *, min_soc: float, max_soc: float,
              capacity_kwh: float) -> List[Tuple[float, float]]:
    """Hourly SOC walk over the candidate hours: a charge hour stores charges[i] kWh (grid serves the load), a
    worth-covering hour discharges its forecast load down to Min SOC, any other hour holds.
    Returns [(SOC after the hour, grid-bought shortfall kWh)] per candidate."""
    soc, cap, out = float(soc), float(capacity_kwh), []
    for i, h in enumerate(cand):
        short = 0.0
        if i in charges:
            soc = min(float(max_soc), soc + charges[i] / cap * 100.0)
        elif worth[i]:
            need = float(load_kwh_by_hour.get(h[0], 0.0)) / cap * 100.0
            short = max(0.0, need - max(0.0, soc - float(min_soc))) * cap / 100.0
            soc = max(float(min_soc), soc - need) if soc >= float(min_soc) else soc
        out.append((soc, short))
    return out


def plan_charge_hours(
    hours: List[Tuple[Any, ...]],
    now_dt: datetime,
    current_soc: float,
    load_kwh_by_hour: Dict[datetime, float],
    *,
    min_soc: float,
    max_soc: float,
    capacity_kwh: float,
    voltage: float,
    ceiling_a: float,
    loss_pct: float,
    held: Optional[Dict[datetime, Tuple[datetime, datetime]]] = None,
    estimated_from: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Greedy cheapest-hour grid charging (data only: prices, forecast load, SOC, battery parameters).
      - candidates: the priced hours [(start, end, price, frame)] ending after now_dt;
      - worth covering: an hour whose price > the cheapest candidate price * (1 + loss%) (battery energy beats grid);
      - estimated_from: hours starting at/after it are estimated (beyond the published prices): they may only create
        shortfalls, they are never charge candidates;
      - hourly SOC walk: a charge hour stores its energy (grid serves load), a worth-covering hour discharges its
        forecast load down to Min SOC (the rest is a grid-bought shortfall), any other hour holds;
      - repeat: take the first remaining shortfall hour; charge the cheapest earlier candidate with ceiling energy left
        whose price * (1 + loss%) is below the shortfall hour's price, with only the energy the shortfall it serves
        needs (capped by Reg 108 ceiling * voltage * (1 - loss%) per hour and the room to Max SOC up to that
        shortfall; an hour already chosen grows); a new hour that would store,
        or serve (shortfall from k up to the next hour at most as cheap), less than one TOU_FLOOR_SOC_STEP of capacity
        is not planned; no such candidate -> the shortfall stays grid-bought. Stops when no coverable shortfall is left.
      - held: {hour start: (start, end) of the grid-charge block the inverter already runs in that hour}; they stay
        while they store and serve any shortfall at all, and a partial hour keeps that block while it is long enough (Schmitt-trigger hysteresis: a charge hour is added at one SOC step, dropped only at zero),
        so SOC rounding noise around the step cannot flip the table hourly (flash wear).
      - minute-granular: every charge runs at the Reg 108 ceiling; an hour needing only part of its energy charges
        for ceil(minutes / TOU_SLOT_MINUTE_STEP) * step minutes, contiguous with an adjacent chosen charge hour (so
        they merge into one slot), else at the end of its hour (shortest holding); a running hour from now.
    Returns [{start, end, energy_kwh (stored), charge_a (Reg 108 ceiling), price}] sorted by start.
    """
    cap = float(capacity_kwh)
    ceiling = min(float(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A), float(ceiling_a))
    keep = 1.0 - float(loss_pct) / 100.0
    max_kwh = ceiling * float(voltage) / 1000.0 * keep
    cand = [h for h in hours if h[1] > now_dt]
    if not cand or cap <= 0 or max_kwh <= 0:
        return []
    published = [estimated_from is None or h[0] < estimated_from for h in cand]
    cheapest = min(h[2] for h, p in zip(cand, published) if p)
    worth = [h[2] > cheapest * (1.0 + float(loss_pct) / 100.0) for h in cand]
    eps = _KWH_EPS
    step_kwh = float(energy_defaults.TOU_FLOOR_SOC_STEP) / 100.0 * cap
    # the running hour only has its remaining fraction left: scale its capacity and its forecast load
    frac = [min(1.0, max(0.0, (h[1] - now_dt) / (h[1] - h[0]))) for h in cand]
    load_kwh_by_hour = dict(load_kwh_by_hour)
    for h, f in zip(cand, frac):
        if h[0] in load_kwh_by_hour:
            load_kwh_by_hour[h[0]] = float(load_kwh_by_hour[h[0]]) * f

    walk = lambda charges: _soc_walk(cand, worth, charges, current_soc, load_kwh_by_hour, min_soc=min_soc,
                                     max_soc=max_soc, capacity_kwh=cap)

    charges: Dict[int, float] = {}
    skipped = set()
    for _ in range(len(cand) * len(cand)):
        tl = walk(charges)
        k = next((i for i, x in enumerate(tl) if x[1] > eps and i not in skipped), None)
        if k is None:
            break
        opts = sorted((cand[i][2], i) for i in range(k)
                      if charges.get(i, 0.0) < max_kwh * frac[i] - eps and published[i]
                      and cand[i][2] * (1.0 + float(loss_pct) / 100.0) < cand[k][2])
        for _, i in opts:
            # the shortfall this hour serves: from k up to the next hour at most as cheap (that one serves the rest)
            stop = next((j for j in range(k + 1, len(cand)) if cand[j][2] <= cand[i][2]), len(cand))
            served = math.fsum(x[1] for x in tl[k:stop])
            # need-sized: only the energy that shortfall needs, capped by the hour's ceiling energy and the room to Max
            e = min(max_kwh * frac[i] - charges.get(i, 0.0), (float(max_soc) - max(x[0] for x in tl[i:k])) / 100.0 * cap,
                    served)
            # hysteresis (flash wear, no "keep at Max" trickle): a new hour that stores, or serves, less than one
            # TOU_FLOOR_SOC_STEP of capacity is not worth a charge slot - that small shortfall stays grid-bought; an
            # hour already chosen (or held on the inverter) may grow by any amount
            least = eps if i in charges or (held and cand[i][0] in held) else step_kwh - eps
            if e >= least and served >= least:
                charges[i] = charges.get(i, 0.0) + e
                break
        else:
            skipped.add(k)
    step = timedelta(minutes=max(1, int(energy_defaults.TOU_SLOT_MINUTE_STEP)))
    out = []
    for i, e in sorted(charges.items()):
        a, b = cand[i][0], cand[i][1]
        span = step * math.ceil((e - _KWH_EPS) / max_kwh * 3600.0 / step.total_seconds())
        hb = held.get(a) if isinstance(held, dict) else None
        if hb is not None and span <= hb[1] - max(hb[0], now_dt) + step:
            # the block already on the inverter still stores enough, within one TOU_SLOT_MINUTE_STEP of the need
            # (need drift between re-syncs): same table (flash wear)
            # ponytail: span <= held + step accepts up to one step of under-charge (<= step/60 * max_kwh); the next
            # re-sync grows the block only once the need exceeds that, tighten by dropping "+ step" if it matters
            a, b = hb
        elif a < now_dt:  # running hour: from now (start snapped down to the step grid)
            s0 = a + (now_dt - a) // step * step
            if s0 + span < b:
                a, b = s0, s0 + span
        elif span < b - a:
            joined_prev = (i - 1) in charges and cand[i - 1][1] == a
            joined_next = (i + 1) in charges and cand[i + 1][0] == b
            a, b = (a, a + span) if joined_prev and not joined_next else (b - span, b)
        out.append({"start": a, "end": b, "energy_kwh": e, "charge_a": int(round(ceiling)), "price": cand[i][2]})
    return out


def plan_rolling_tou(
    frames: List[Dict[str, Any]],
    now_dt: datetime,
    current_soc: float,
    profile: Optional[Dict[str, List[float]]],
    *,
    min_soc: float,
    max_soc: float,
    capacity_kwh: float,
    voltage: float,
    ceiling_a: float,
    loss_pct: float,
    calibration_active: bool = False,
    charge_windows: Optional[List[Dict[str, Any]]] = None,
    max_discharge_current: Optional[float] = None,
    default_power_w: Optional[int] = None,
    live_kw: Optional[float] = None,
    active_slots: Optional[List[Dict[str, Any]]] = None,
    # ponytail: off - the daily table is a clock circle, so charges justified only by estimated hours get merged
    # with today's pattern on the circle (clock-circle merge) and cost writes without a known price; True to re-test
    estimate_beyond_horizon: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    One rolling TOU plan from now_dt to the end of the published prices (no today/tomorrow split).
      - Peaks per price day: analyse_day_price_profile on each Europe/Warsaw day's own prices (gross retail, Rule 12).
      - Grid charge: plan_charge_hours (greedy cheapest hours covering the forecast load of the worth-covering
        hours; active_slots = the table on the inverter: its grid-charge hours in the next 24 h are "held", see
        plan_charge_hours); calibration or no load profile fills to the top in the cheapest hours. charge_windows overrides
        them ([{start, end (aware), charge_a}]). A charge hour running now keeps its hour start (table anchor).
      - Load: the hourly profile (build_load_profile) via load_forecast.
      - Floors in one SOC walk (project_soc_timeline, Deye floor semantics): charge -> top; worth-covering hour
        (price > cheapest * (1 + loss%)) -> Min SOC; other hours -> hold at the projected SOC (snapped down).
    Returns {"plan_timeline": [{start, end (ISO Z), role, target_soc, soc_start, soc_end, charge_a, price}],
             "deye_tou_schedule": 6 slots for 24 h from the start of the behaviour running now (the table repeats
                                  daily; beyond the published prices it repeats today's pattern; rewritten by the
                                  hourly re-sync only when it changes),
             "horizon_end": ISO Z (end of the PUBLISHED prices)}, or None without prices ending after now.
      - estimate_beyond_horizon (greedy plans only): one more day after the last published hour, each hour priced as
        the same Warsaw clock hour of the last published day, load from the profile. Those hours (timeline entries
        "estimated": True) only create shortfalls that justify charging in published hours: never charge hours and
        never in the written table (which keeps today's pattern beyond the published prices).
    """
    now = now_dt if now_dt.tzinfo else now_dt.replace(tzinfo=WARSAW_TZ)
    seen: Dict[datetime, Tuple[Any, ...]] = {}
    for f in frames or []:
        p = ((f.get("metrics") or {}).get("pricing") or {}) if isinstance(f, dict) else {}
        a = _iso_dt(f.get("start")) if isinstance(f, dict) else None
        if a is None or a.tzinfo is None or (p.get("price_gross") is None and p.get("price_net") is None):
            continue
        b = _iso_dt(f.get("end")) or a + timedelta(hours=1)
        seen[a] = (a, b, retail_gross_price(f), f)
    hours = [seen[k] for k in sorted(seen)]
    if not hours or hours[-1][1] <= now:
        return None
    horizon_end = hours[-1][1]
    est: List[Tuple[Any, ...]] = []
    one_day = timedelta(hours=24)
    if estimate_beyond_horizon and charge_windows is None:
        last = {h[0].astimezone(WARSAW_TZ).hour: h for h in hours[-24:]}
        for a, b, price, f in hours[-24:]:
            src = last.get((a + one_day).astimezone(WARSAW_TZ).hour, (a, b, price, f))
            est.append((a + one_day, b + one_day, src[2], src[3]))
    all_hours = hours + est
    plan_end = all_hours[-1][1]
    top = float(100 if calibration_active else max_soc)  # BMS calibration charges to 100 % SOC
    ceiling = min(float(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A), float(ceiling_a))
    cap = float(capacity_kwh)

    # 1. per price day: peak/valley segments (labels only)
    by_day: Dict[Any, List[Tuple[Any, ...]]] = {}
    for h in all_hours:
        by_day.setdefault(h[0].astimezone(WARSAW_TZ).date(), []).append(h)
    seg_of: Dict[datetime, Tuple[str, Any, float]] = {}
    for day, hs in by_day.items():
        for k, sg in enumerate(analyse_day_price_profile([h[3] for h in hs])["segments"]):
            for h in hs[sg["start_index"]:sg["end_index"]]:
                seg_of[h[0]] = (sg["kind"], (day.isoformat(), k) if sg["kind"] == "peak" else None, sg["avg"])
    charge_kw = ceiling * float(voltage) / 1000.0 / (1.0 + float(loss_pct) / 100.0)
    local_now = now.astimezone(WARSAW_TZ)
    day_start = datetime(local_now.year, local_now.month, local_now.day, tzinfo=WARSAW_TZ)
    plan_from = min(now, max(hours[0][0], day_start))
    hour_key = lambda t: t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rates: Dict[datetime, float] = {}
    for a, b, live in ((plan_from, now, None), (now, plan_end, live_kw)):
        fc = load_forecast(a, b, profile, live)
        for i, x in enumerate(fc):
            x_end = fc[i + 1]["start"] if i + 1 < len(fc) else b
            rates[hour_key(x["start"])] = x["kwh"] / ((x_end - x["start"]).total_seconds() / 3600.0)
    has_load = bool(profile) and any(v > 0 for v in rates.values())
    cand = [h for h in hours if h[1] > now]
    cheapest = min(h[2] for h in cand)
    worth = lambda price: price > cheapest * (1.0 + float(loss_pct) / 100.0)
    try:  # the table on the inverter (6 slots): its grid-charge hours are held, see plan_charge_hours
        rows = _tou_minute_rows(active_slots) if active_slots else []
    except (ValueError, TypeError, AttributeError):
        rows = []

    # 2. grid charge hours: explicit windows, else greedy cheapest hours (plan_charge_hours); calibration or no load
    # profile fills to the top in the cheapest hours (price order)
    auto = charge_windows is None
    if auto:
        if calibration_active or not has_load:
            # fill to the top: cheapest hours first (full ceiling hours) until the SOC walk reaches the top
            full = ceiling * float(voltage) / 1000.0 * (1.0 - float(loss_pct) / 100.0)
            loads = {h[0]: rates.get(hour_key(h[0]), 0.0) for h in cand}
            flags = [worth(h[2]) for h in cand]
            chosen: Dict[int, float] = {}
            # no maintenance trickle: within one TOU_FLOOR_SOC_STEP of the top nothing is charged
            near_top = float(current_soc) > top - float(energy_defaults.TOU_FLOOR_SOC_STEP)
            for i in sorted(range(len(cand)), key=lambda j: (cand[j][2], cand[j][0])):
                if near_top or full <= 0 or cap <= 0 or max([float(current_soc)] + [x[0] for x in _soc_walk(
                        cand, flags, chosen, current_soc, loads, min_soc=min_soc, max_soc=top,
                        capacity_kwh=cap)]) >= top - _KWH_EPS:
                    break
                chosen[i] = full
            picks = [{"start": cand[i][0], "end": cand[i][1], "energy_kwh": e, "charge_a": int(round(ceiling))}
                     for i, e in sorted(chosen.items())]
        else:
            held: Dict[datetime, Tuple[datetime, datetime]] = {}
            step = timedelta(minutes=max(1, int(energy_defaults.TOU_SLOT_MINUTE_STEP)))
            for h in cand if rows else []:
                if h[0] >= now + timedelta(hours=24):
                    continue
                # the grid-charge minutes the inverter runs in this hour (a partial block may start mid-hour)
                on = [t for t in (h[0] + k * step for k in range(int((h[1] - h[0]) / step)))
                      if _tou_row_at(rows, t.astimezone(WARSAW_TZ).hour * 60 + t.astimezone(WARSAW_TZ).minute)[1]]
                if on:
                    held[h[0]] = (on[0], on[-1] + step)
            picks = plan_charge_hours(all_hours, now, current_soc,
                                      {h[0]: rates.get(hour_key(h[0]), 0.0) for h in all_hours},
                                      min_soc=min_soc, max_soc=top, capacity_kwh=cap, voltage=voltage,
                                      ceiling_a=ceiling, loss_pct=loss_pct, held=held,
                                      estimated_from=horizon_end if est else None)
        floor_a = float(energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A)
        charge_windows = [{"start": p["start"], "end": p["end"],
                           "charge_kw": p["energy_kwh"] / ((p["end"] - p["start"]).total_seconds() / 3600.0),
                           "charge_a": int(round(min(ceiling, max(floor_a, p["charge_a"]))))} for p in picks]

    # 3. cells: hour pieces from the start of today's price day (the part before now only fills the table's tail
    # beyond the known prices with today's pattern - the table repeats daily), cut at now and at charge boundaries
    charges = [c for c in charge_windows if c["end"] > plan_from and c["start"] < plan_end]
    cuts = {plan_from, now, horizon_end, plan_end} | {t for h in all_hours for t in h[:2] if plan_from < t < plan_end} \
        | {t for c in charges for t in (c["start"], c["end"]) if plan_from < t < plan_end}
    cells: List[Dict[str, Any]] = []
    srt = sorted(cuts)
    for a, b in zip(srt, srt[1:]):
        h = next((h for h in reversed(all_hours) if h[0] <= a), hours[0])
        kind, pid, avg = seg_of.get(h[0], ("valley", None, h[2]))
        ch = next((c for c in charges if c["start"] <= a < c["end"]), None)
        cells.append({"start": a, "end": b, "price": h[2], "kind": "charge" if ch else kind, "peak": pid, "seg_avg": avg,
                      "charge": ch, "target_soc": int(min_soc), "charge_a": 0, "load_kw": rates.get(hour_key(a), 0.0),
                      "estimated": a >= horizon_end})

    # 4. floors. Greedy charges: one SOC walk - charge -> grid charge to the top; worth-covering hour -> Min SOC (the
    # battery serves the load); any other hour -> hold at the projected SOC (snapped down, the grid serves the load).
    # Explicit charge_windows (CLI / generate_deye_tou_schedule): the segment floors of _assign_cell_floors.
    soc, proj = float(current_soc), []
    if not auto:
        _assign_cell_floors(cells, min_soc=min_soc, top=top, capacity_kwh=cap, loss_pct=loss_pct, has_load=has_load)
        proj = project_soc_timeline(cells, soc, 0.0, now, min_soc=min_soc, max_soc=top, capacity_kwh=cap,
                                    charge_kw=charge_kw)
    for c in cells if auto else []:
        ch = c["charge"]
        if ch is not None:
            c.update(role="charge", target_soc=int(top), grid_charge=True, charge_a=int(ch["charge_a"]))
            if "charge_kw" in ch:
                c["charge_kw"] = ch["charge_kw"]
        else:
            # the rest of an hour with a (need-sized) charge block holds at the projected SOC (the grid serves the
            # load, as plan_charge_hours' SOC walk assumes for a charge hour) whatever `cheapest` (the remaining
            # hours) says at the next re-sync: the same block gives the same hold, never flipping hourly (flash wear)
            hk = hour_key(c["start"])
            if any(x["start"] < hk + timedelta(hours=1) and x["end"] > hk for x in charges):
                tgt = int(min_soc) if c["end"] <= now else _snap_floor(soc, min_soc, top)
            else:
                tgt = int(min_soc) if worth(c["price"]) or c["end"] <= now else _snap_floor(soc, min_soc, top)
            role = "peak" if c["kind"] == "peak" and tgt <= int(min_soc) else ("hold" if tgt > int(min_soc) else "drain")
            c.update(role=role, target_soc=tgt, grid_charge=False, charge_a=0)
        if c["end"] > now:
            p = project_soc_timeline([c], soc, 0.0, now, min_soc=min_soc, max_soc=top, capacity_kwh=cap,
                                     charge_kw=charge_kw)[0]
            soc = p["soc_end"]
            proj.append(p)
    k_now = next(i for i, c in enumerate(cells) if c["end"] > now)
    past, cells = cells[:k_now], cells[k_now:]
    entries = _merge_timeline(cells, proj)
    day = one_day
    tail = [dict(e, start=e["start"] + day, end=e["end"] + day)
            for e in _merge_timeline(past, [{"soc_start": 0.0, "soc_end": 0.0}] * len(past))
            if e["end"] + day > horizon_end]

    # 4. the written 6-slot table: 24 h from the anchor below, projected onto clock minutes once here
    _, eff_volts, _, eff_dis = resolve_battery_hardware_parameters(
        battery_capacity_kwh=cap, nominal_voltage=voltage, max_discharge_current=max_discharge_current)
    clamp_w = lambda w: min(energy_defaults.INVERTER_MAX_POWER_W, max(energy_defaults.INVERTER_MIN_POWER_W, int(round(w, -1))))
    if max_discharge_current is not None and float(max_discharge_current) > 0:
        base_w = clamp_w(float(max_discharge_current) * eff_volts)
    elif default_power_w:
        base_w = int(default_power_w)
    elif eff_dis > 0 and eff_volts > 0:
        base_w = clamp_w(eff_dis * eff_volts)
    else:
        base_w = energy_defaults.DEFAULT_TRANSFER_POWER_W
    # table anchor: start of the behaviour running now (its run through today's earlier hours, or the hour start of a
    # top-up running now), so hourly re-syncs write the same table while that behaviour lasts (flash wear)
    beh = lambda c: (c["role"], c["target_soc"], c["charge_a"])
    h0 = now
    for c in reversed(past):
        if beh(c) != beh(cells[0]):
            break
        h0 = c["start"]
    if rows:
        # with the inverter's table known: the start of its slot running now, while that slot already does what the
        # plan does now (a behaviour boundary, never `now`; the window then keeps every planned hour of the next day)
        loc = now.astimezone(WARSAW_TZ)
        m = loc.hour * 60 + loc.minute
        r = _tou_row_at(rows, m)
        midnight = datetime(loc.year, loc.month, loc.day, tzinfo=WARSAW_TZ) - (timedelta(days=1) if r[0] > m else timedelta(0))
        same = r[1] == bool(cells[0]["grid_charge"]) and \
            abs(r[2] - int(cells[0]["target_soc"])) <= (0 if r[1] else int(energy_defaults.TOU_FLOOR_SOC_STEP))
        if same:
            h0 = min(now, midnight + timedelta(minutes=r[0]))
    if rows and tail:
        # beyond the known prices nothing is planned: keep what the inverter's table already does there (its own
        # clock slots), so the guessed tail never rewrites the flash on its own
        price_at = {h[0]: h[2] for h in hours}
        tail, t, t_end = [], horizon_end, h0 + day
        while t < t_end:
            loc = t.astimezone(WARSAW_TZ)
            m = loc.hour * 60 + loc.minute
            r = _tou_row_at(rows, m)
            nxt = next((x[0] for x in rows if x[0] > m), rows[0][0] + 24 * 60)
            midnight = datetime(loc.year, loc.month, loc.day, tzinfo=WARSAW_TZ)
            b = min(t_end, max(t + timedelta(minutes=1), midnight + timedelta(minutes=nxt)))
            tail.append({"start": t, "end": b, "grid_charge": r[1], "target_soc": r[2],
                         "charge_a": int(round(ceiling)) if r[1] else 0,
                         "role": "charge" if r[1] else ("hold" if r[2] > int(min_soc) else "drain"),
                         "price": price_at.get(hour_key(t - day).astimezone(t.tzinfo), hours[-1][2]),
                         "soc_start": 0.0, "soc_end": 0.0})
            t = b
    if tail:
        tail[0]["start"] = max(tail[0]["start"], horizon_end)
    segs = _fit_tou_table([e for e in entries if not e["estimated"]] + tail, h0, int(energy_defaults.TOU_SLOT_COUNT), rows)
    slots = _tou_table_slots(segs, min_soc=min_soc, voltage=eff_volts, discharge_a=eff_dis, base_power_w=base_w,
                             calibration_active=calibration_active)
    timeline = [{
        "start": _iso_z(e["start"]), "end": _iso_z(e["end"]), "role": e["role"], "target_soc": e["target_soc"],
        "soc_start": round(e["soc_start"], 1), "soc_end": round(e["soc_end"], 1), "charge_a": e["charge_a"],
        "price": e["price"], "estimated": e["estimated"],
    } for e in entries]
    return {"plan_timeline": timeline, "deye_tou_schedule": slots, "horizon_end": _iso_z(horizon_end)}


def _window_minute_intervals(w: Any) -> List[Tuple[int, int]]:
    """[(start_m, end_m)] Warsaw-minute intervals of a window dict ('HH:MM' or ISO; disjoint slots kept)."""
    if not isinstance(w, dict) or "error" in w or not w.get("start"):
        return []
    if w.get("is_consecutive", True) is False and w.get("slots"):
        bounds = [[x.strip() for x in str(sl).split(" - ")] for sl in w["slots"]]
    else:
        bounds = [[format_hhmm(w.get("start")), format_hhmm(w.get("end"))]]
    out = []
    for pair in bounds:
        mins = [hhmm_to_minutes(x) for x in pair]
        if len(mins) == 2 and None not in mins:
            a, b = mins[0], mins[1] if mins[1] > mins[0] else mins[1] + 1440
            out += [(a, min(b, 1440))] + ([(0, b - 1440)] if b > 1440 else [])
    return out


def generate_deye_tou_schedule(
    current_soc: Optional[int] = None,
    max_soc: Optional[int] = None,
    min_soc: Optional[int] = None,
    best_pb_window: Optional[Dict[str, Any]] = None,
    best_sell_window: Optional[Dict[str, Any]] = None,
    calibration_active: bool = False,
    default_power_w: Optional[int] = None,
    max_charge_current: Optional[float] = None,
    max_discharge_current: Optional[float] = None,
    battery_capacity_kwh: Optional[float] = None,
    nominal_voltage: Optional[float] = None,
    loss_buffer_pct: Optional[float] = None,
    best_discharge_windows: Optional[Any] = None,
    pricing_data: Optional[Dict[str, Any]] = None,
    load_kw: Optional[float] = None,
    now_dt: Optional[datetime] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Thin single-day wrapper over plan_rolling_tou for explicit windows (CLI tools, tests): the Warsaw day of the
    pricing frames (else of now_dt / now) planned from now_dt (inside that day, else its 00:00) with current_soc. Prices: pricing_data frames, else a
    synthetic day whose explicit peak windows (best_discharge_windows / best_sell_window) price above the rest. The
    main charge is best_pb_window (none -> no main charge); its Reg 108 target = ceiling * min_h / T (energy need).
    load_kw: a flat hourly profile (snapped to LOAD_ESTIMATE_STEP_KW); None/0 -> no top-ups, no reserves.
    Returns the 6 TOU slots, or None without price data.
    """
    priced = [f for f in (pricing_data.get("frames") or []) if _pricing_frame_ok(f)] if isinstance(pricing_data, dict) else []
    ref = _iso_dt(priced[0].get("start")) if priced else None
    ref = (ref or now_dt or datetime.now(WARSAW_TZ)).astimezone(WARSAW_TZ)
    day0 = datetime(ref.year, ref.month, ref.day, tzinfo=WARSAW_TZ)
    nxt = day0 + timedelta(hours=36)
    day1 = datetime(nxt.year, nxt.month, nxt.day, tzinfo=WARSAW_TZ)
    at = lambda m: (day0.astimezone(timezone.utc) + timedelta(minutes=m)).astimezone(WARSAW_TZ) if m < 1440 else day1
    if not priced:
        source = best_discharge_windows or best_sell_window
        items = source["peaks"] if isinstance(source, dict) and isinstance(source.get("peaks"), list) else (
            source if isinstance(source, list) else [source])
        peak_ivs = [iv for w in items for iv in _window_minute_intervals(w)]
        if not peak_ivs:
            return None
        priced = []
        t = day0.astimezone(timezone.utc)
        while t < day1.astimezone(timezone.utc):
            loc = t.astimezone(WARSAW_TZ)
            m = loc.hour * 60 + loc.minute
            price = 2.0 if any(a <= m < b for a, b in peak_ivs) else 1.0
            priced.append({"start": _iso_z(t), "end": _iso_z(t + timedelta(hours=1)), "metrics": {"pricing": {"price_gross": price}}})
            t += timedelta(hours=1)

    cap, volts, _, dis_a = resolve_battery_hardware_parameters(
        battery_capacity_kwh=battery_capacity_kwh, nominal_voltage=nominal_voltage,
        max_charge_current=max_charge_current, max_discharge_current=max_discharge_current)
    top, low, _, soc, loss = resolve_battery_dispatch_parameters(
        max_soc=max_soc, min_soc=min_soc, current_soc=current_soc, loss_buffer_pct=loss_buffer_pct)
    ceiling = float(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A)
    ivs = _window_minute_intervals(best_pb_window)
    charge_h = sum(b - a for a, b in ivs) // 60
    min_h = charge_window_min_hours(soc, top, cap, volts, ceiling, loss)
    amps = int(round(ceiling)) if calibration_active else charge_current_for_window(min_h, charge_h or min_h, ceiling)
    charges = [{"start": at(a), "end": at(b), "charge_a": amps} for a, b in ivs]
    load = snap_load_kw(load_kw)
    profile = {k: [load] * 24 for k in ("weekday", "weekend")} if load else None
    start = (now_dt or day0).astimezone(WARSAW_TZ)
    start = start if day0 <= start < day1 else day0  # current_soc is valid at now_dt (a table for another day: 00:00)
    plan = plan_rolling_tou(priced, start, soc, profile, min_soc=low, max_soc=top, capacity_kwh=cap, voltage=volts,
                            ceiling_a=ceiling, loss_pct=loss, calibration_active=calibration_active,
                            charge_windows=charges, max_discharge_current=max_discharge_current,
                            default_power_w=default_power_w)
    return plan["deye_tou_schedule"] if plan else None


# Core engine dispatch alias
calculate_deye_tou_schedule = generate_deye_tou_schedule


LAST_TOU_SYNC_PATHS = [
    "/config/.deye_tou_last_sync.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deye_tou_last_sync.json"),
    "/tmp/.deye_tou_last_sync.json",
]


def save_last_tou_sync_result(result_data: Dict[str, Any]) -> None:
    """Persists the last TOU synchronization outcome to disk for Home Assistant UI feedback."""
    for p in LAST_TOU_SYNC_PATHS:
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(result_data, f, indent=2, ensure_ascii=False)
            break
        except Exception:
            continue


def _tou_minute_rows(slots: List[Dict[str, Any]]) -> List[Tuple[int, bool, int, int]]:
    """[(clock minute, grid_charge, target_soc, power_w)] of a 6-slot table sorted by minute (raises on bad times)."""
    rows = []
    for x in slots:
        hh, mm = str(x.get("time", "")).strip().split(":")
        rows.append((int(hh) * 60 + int(mm), bool(x.get("grid_charge")), int(x.get("target_soc", 0)),
                     int(x.get("power_w", energy_defaults.DEFAULT_TRANSFER_POWER_W))))
    return sorted(rows)


def _tou_row_at(rows: List[Tuple[int, bool, int, int]], minute: int) -> Tuple[int, bool, int, int]:
    """The table row in force at a clock minute (the last slot runs on through midnight)."""
    return next((r for r in reversed(rows) if r[0] <= minute), rows[-1])


def are_tou_slots_identical(new_slots: List[Dict[str, Any]], active_slots: List[Dict[str, Any]]) -> bool:
    """
    True when two 6-slot TOU tables behave the same at every minute of the daily circle: grid_charge, target_soc
    (non-charge minutes within +-TOU_FLOOR_SOC_STEP) and power_w (within +-TOU_POWER_DIFF_TOLERANCE_W). Slot
    boundaries that only split one behaviour (same behaviour on both sides) are not a difference, so a re-sync that
    moves such a boundary does not rewrite the inverter flash.
    """
    if not isinstance(new_slots, list) or not isinstance(active_slots, list):
        return False
    if len(new_slots) != 6 or len(active_slots) != 6:
        return False
    try:
        tables = [_tou_minute_rows(new_slots), _tou_minute_rows(active_slots)]
    except (ValueError, TypeError, AttributeError):
        return False
    step = int(energy_defaults.TOU_FLOOR_SOC_STEP)
    for m in {r[0] for t in tables for r in t}:
        (_, g1, s1, p1), (_, g2, s2, p2) = _tou_row_at(tables[0], m), _tou_row_at(tables[1], m)
        # charge targets exactly; other floors are projected from a drifting SOC (step hysteresis)
        if g1 != g2 or abs(s1 - s2) > (0 if g1 else step) \
                or abs(p1 - p2) > energy_defaults.TOU_POWER_DIFF_TOLERANCE_W:
            return False
    return True


def check_and_apply_charge_current_modulation(
    slots: List[Dict[str, Any]],
    host: Optional[str] = None,
    port: Optional[int] = None,
    base_charge_current: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Sets Register 108 (Max A Charge) for the current Warsaw time from the TOU grid-charge slots:
    - Inside a grid-charge slot: that slot's target_charge_current_a (ceiling * min_h / T from the energy need).
    - Outside every grid-charge slot: the BATTERY_CHARGE_CURRENT_MAX_CEILING_A baseline (Rule 12: no pre-set of the
      next charge's current; a reduced-current charge may run up to one sync interval at the ceiling before the next
      sync inside it modulates Reg 108).
    Always clamped to [BATTERY_CHARGE_CURRENT_MIN_FLOOR_A, BATTERY_CHARGE_CURRENT_MAX_CEILING_A]; the live register
    value is never used as a base (no ratchet). Idempotent write via set_charge_current_with_fallback.
    """
    now_warsaw = datetime.now(WARSAW_TZ)
    now_m = now_warsaw.hour * 60 + now_warsaw.minute
    ceiling_curr = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))
    floor_curr = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A))

    windows: List[Tuple[int, int, int]] = []  # (start_m, end_m, target_a); end_m > 1440 when it runs past midnight
    for idx, s in enumerate(slots):
        if s.get("grid_charge"):
            start_m = hhmm_to_minutes(s.get("time"))
            end_m = hhmm_to_minutes(slots[(idx + 1) % len(slots)].get("time"))
            if start_m is None or end_m is None:
                continue
            if end_m <= start_m:
                end_m += 1440
            target = s.get("target_charge_current_a")
            windows.append((start_m, end_m, ceiling_curr if target is None else int(target)))

    active = next((w for w in windows if w[0] <= now_m < w[1] or w[0] <= now_m + 1440 < w[1]), None)
    desired_a = active[-1] if active is not None else ceiling_curr

    desired_a = max(floor_curr, min(ceiling_curr, desired_a))

    mod_res = set_charge_current_with_fallback(host, port, desired_a)

    mod_res["in_charging_window"] = active is not None
    mod_res["desired_a"] = desired_a
    return mod_res


def load_household_profile() -> Optional[Dict[str, List[float]]]:
    """Hourly household load profile from the Deye history CACHE FILE only (never a network call: the price sensors
    must stay fast; the daily `deye_cloud_client.py --fetch-history` job fills the cache). None when the cache is
    missing/unusable -> the planner adds no top-ups and no reserves."""
    try:
        import deye_cloud_client
        return build_load_profile(deye_cloud_client.load_cached_history())
    except Exception as e:
        print(f"[load-profile] no Deye load history ({e}) -> no top-ups / reserves", file=sys.stderr)
        return None


def payload_pricing_frames(payload: Any) -> List[Dict[str, Any]]:
    """Priced frames (pricing only) of a consolidated payload: the rolling planner's price input."""
    if not isinstance(payload, dict):
        return []
    return payload.get("pricing_frames") or []


def sync_deye_inverter_tou_schedule(
    host: Optional[str] = None,
    port: Optional[int] = None,
    current_soc: Optional[int] = None,
    max_soc: Optional[int] = None,
    min_soc: Optional[int] = None,
    shutdown_soc: Optional[int] = None,
    calibration_active: bool = False,
    api_key: Optional[str] = None,
    installation: str = "dol",
    cache_dir: str = DEFAULT_CACHE_DIR,
    force: bool = False,
    max_charge_current: Optional[float] = None,
    max_discharge_current: Optional[float] = None,
    battery_capacity_kwh: Optional[float] = None,
    nominal_voltage: Optional[float] = None,
    loss_buffer_pct: Optional[float] = None,
    sell_target_hours: Any = None,
    ev_target_hours: Any = "auto",
) -> Dict[str, Any]:
    """
    Recalculates the rolling TOU plan (plan_rolling_tou: now -> end of the published prices) and uploads its 6-slot
    table for [hour(now), +24 h) to the Deye hybrid inverter via the Solarman V5 client (Deye Cloud fallback).
    Idempotent diff check: if the table matches the active inverter schedule the write is skipped (flash wear, cloud
    rate limits); force=True bypasses it. Register 108 (Max A Charge) follows the active grid-charge slot, else the
    next grid-charge entry of the timeline, else the ceiling baseline.
    """
    global solarman_v5_client
    if solarman_v5_client is None:
        try:
            import solarman_v5_client
        except ImportError:
            pass

    if solarman_v5_client is not None:
        host = solarman_v5_client.get_configured_inverter_ip(host)
        port = solarman_v5_client.get_configured_inverter_port(port)
    else:
        host = host or energy_defaults.DEFAULT_INVERTER_IP
        port = port or energy_defaults.DEFAULT_INVERTER_PORT

    live_telem = {}
    if solarman_v5_client is not None:
        try:
            live_telem = solarman_v5_client.read_inverter_telemetry(host=host, port=port)
        except Exception as e:
            live_telem = {"success": False, "error": str(e)}

    # Resolve SOC parameters (prefer explicit arguments, fallback to live inverter read, fallback to standard defaults)
    inv_cfg = live_telem.get("config", {}) if isinstance(live_telem, dict) else {}
    inv_tlm = live_telem.get("telemetry", {}) if isinstance(live_telem, dict) else {}
    active_tou = live_telem.get("active_tou", {}) if isinstance(live_telem, dict) else {}

    (
        eff_max_soc,
        eff_min_soc,
        eff_shutdown_soc,
        eff_current_soc,
        eff_loss_buffer_pct,
    ) = resolve_battery_dispatch_parameters(
        max_soc=max_soc,
        min_soc=min_soc,
        shutdown_soc=shutdown_soc,
        current_soc=current_soc,
        loss_buffer_pct=loss_buffer_pct,
        inv_cfg=inv_cfg,
        inv_tlm=inv_tlm,
    )
    eff_capacity_kwh, eff_nominal_voltage, eff_max_charge_current, eff_max_discharge_current = resolve_battery_hardware_parameters(
        battery_capacity_kwh=battery_capacity_kwh,
        nominal_voltage=nominal_voltage,
        max_charge_current=max_charge_current,
        max_discharge_current=max_discharge_current,
        inv_cfg=inv_cfg,
        inv_tlm=inv_tlm,
    )

    # Prices: the consolidated cache (same Warsaw day, same target hours, horizon not yet over), else a fresh fetch
    cache_file = get_cache_file_path(installation, cache_dir)
    sell_target_hours = parse_target_hours(sell_target_hours, energy_defaults.SELL_TARGET_HOURS)
    ev_target_hours = parse_target_hours(ev_target_hours, "auto")
    source_data = read_cache(cache_file)
    if isinstance(source_data, dict) and not cache_is_reusable(source_data, sell_target_hours, ev_target_hours):
        source_data = None
    if not payload_pricing_frames(source_data):
        resolved_key, _ = resolve_api_key(installation=installation, explicit_key=api_key)
        if resolved_key:
            try:
                fresh_data = fetch_consolidated_data(
                    api_key=resolved_key, installation=installation, cache_dir=cache_dir,
                    sell_target_hours=sell_target_hours, ev_target_hours=ev_target_hours,
                )
                if isinstance(fresh_data, dict):
                    source_data = fresh_data
            except Exception:
                pass

    err_msg = None
    if isinstance(source_data, dict) and source_data.get("price_coverage_complete") is False:
        # Never derive/write TOU from a partial price day; keep the inverter's existing schedule
        err_msg = source_data.get("tou_error") or "Niepełne ceny Pstryk na dziś"
    elif not payload_pricing_frames(source_data):
        err_msg = "Brak poprawnych danych cenowych Pstryk"
    plan = None
    if err_msg is None:
        plan = plan_rolling_tou(
            payload_pricing_frames(source_data), datetime.now(WARSAW_TZ), eff_current_soc, load_household_profile(),
            min_soc=eff_min_soc, max_soc=eff_max_soc, capacity_kwh=eff_capacity_kwh, voltage=eff_nominal_voltage,
            ceiling_a=energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A, loss_pct=eff_loss_buffer_pct,
            calibration_active=calibration_active, max_discharge_current=eff_max_discharge_current,
            active_slots=active_tou.get("slots") if isinstance(active_tou, dict) and active_tou.get("enabled", True) else None,
        )
        if not plan or not plan.get("deye_tou_schedule"):
            err_msg = "brak danych cenowych do wyliczenia harmonogramu"
    if err_msg is not None:
        err_dict = {
            "success": False,
            "error": f"Nie można zsynchronizować TOU z falownikiem: {err_msg}. Przerwano zapis, aby zachować dotychczasowy harmonogram.",
        }
        save_last_tou_sync_result(err_dict)
        return err_dict
    slots = plan["deye_tou_schedule"]

    # Reg 108: the active grid-charge slot's current, else the ceiling (Rule 12)
    charge_mod_res = check_and_apply_charge_current_modulation(
        slots=slots, host=host, port=port)

    # Idempotent diff check: verify if active inverter slots already match newly calculated schedule
    global _DISPATCH_CACHE
    active_slots = active_tou.get("slots", []) if isinstance(active_tou, dict) else []
    active_enabled = active_tou.get("enabled", True) if isinstance(active_tou, dict) else True
    if not force and active_enabled and are_tou_slots_identical(slots, active_slots):
        _DISPATCH_CACHE = None  # identical: the cached inverter table is already right, keep it
        unchanged_res = {
            "success": True,
            "unchanged": True,
            "message": "Harmonogram TOU w falowniku jest identyczny z wyliczonym — pominięto zapis (ochrona pamięci i limitów API).",
            "timestamp": datetime.now().isoformat(),
            "host": host,
            "port": port,
            "calibration_active": calibration_active,
            "soc_limits": {
                "current_soc": eff_current_soc,
                "max_soc": eff_max_soc,
                "min_soc": eff_min_soc,
                "max_charge_current": eff_max_charge_current,
                "max_discharge_current": eff_max_discharge_current,
                "battery_capacity_kwh": eff_capacity_kwh,
                "nominal_voltage": eff_nominal_voltage,
                "loss_buffer_pct": eff_loss_buffer_pct,
            },
            "charge_current_modulation": charge_mod_res,
            "slots": slots,
            "plan_timeline": plan["plan_timeline"],
            "horizon_end": plan["horizon_end"],
        }
        save_last_tou_sync_result(unchanged_res)
        return unchanged_res

    sync_result = {"success": False, "message": "solarman_v5_client module not available", "cloud_attempted": False}
    if solarman_v5_client is not None:
        try:
            sync_result = solarman_v5_client.sync_tou_schedule(slots=slots, host=host, port=port)
        except Exception as e:
            sync_result = {"success": False, "error": str(e), "cloud_attempted": False}

    # Fallback to Deye Cloud OpenAPI only if the local client did not already try it
    # (solarman_v5_client.sync_tou_schedule falls back to the cloud internally when local writes fail).
    if not sync_result.get("success") and sync_result.get("cloud_attempted", True) is False:
        try:
            import deye_cloud_client
            cloud_cfg = deye_cloud_client.get_deye_cloud_config()
            if cloud_cfg.get("api_key") or cloud_cfg.get("app_id") or cloud_cfg.get("email"):
                cloud_res = deye_cloud_client.sync_tou_schedule(slots=slots, cfg=cloud_cfg)
                if isinstance(cloud_res, dict):
                    cloud_res["local_fallback_reason"] = sync_result.get("error") or sync_result.get("message", "Local Modbus failed or port closed")
                    sync_result = cloud_res
        except Exception:
            pass

    if sync_result.get("success"):
        _DISPATCH_CACHE = None
        if solarman_v5_client is not None:
            try:
                cached = solarman_v5_client.load_deye_cache(ignore_ttl=True)
                if cached and isinstance(cached, dict):
                    cached["active_tou"] = {"enabled": True, "slots": slots}
                    solarman_v5_client.save_deye_cache(cached)
            except Exception:
                pass

    final_result = {
        "success": sync_result.get("success", False),
        "unchanged": False,
        "timestamp": datetime.now().isoformat(),
        "host": host,
        "port": port,
        "calibration_active": calibration_active,
        "soc_limits": {
            "current_soc": eff_current_soc,
            "max_soc": eff_max_soc,
            "min_soc": eff_min_soc,
            "shutdown_soc": eff_shutdown_soc,
            "max_charge_current": eff_max_charge_current,
            "max_discharge_current": eff_max_discharge_current,
            "battery_capacity_kwh": eff_capacity_kwh,
            "nominal_voltage": eff_nominal_voltage,
            "loss_buffer_pct": eff_loss_buffer_pct,
        },
        "charge_current_modulation": charge_mod_res,
        "slots": slots,
        "plan_timeline": plan["plan_timeline"],
        "horizon_end": plan["horizon_end"],
        "inverter_response": sync_result,
    }
    save_last_tou_sync_result(final_result)
    return final_result


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


def format_hhmm(iso_str: Optional[str], tz: Optional[Any] = None) -> str:
    """Extracts 'HH:MM' time string in Europe/Warsaw timezone from an ISO timestamp."""
    if not iso_str or not isinstance(iso_str, str):
        return "--:--"
    target_tz = tz or WARSAW_TZ
    try:
        if "Z" in iso_str or "+" in iso_str:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(target_tz)
            return dt.strftime("%H:%M")
        elif "T" in iso_str:
            return iso_str.split("T")[1][:5]
    except Exception:
        pass
    if "T" in iso_str:
        return iso_str.split("T")[1][:5]
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


def warsaw_day_covered(frames: List[Dict[str, Any]], day: Any) -> bool:
    """
    Span-based price coverage of one Europe/Warsaw calendar day: frames must start at/before Warsaw 00:00,
    end at/after the next Warsaw 00:00, and the holes summed over the whole day may total at most 1 h
    (enough for a 25 h DST day returned as 24 frames; also tolerates one missing hour on a normal day).
    Frame counts are not used, so DST days (23/25 h) pass however the API labels them.
    """
    def _p(iso: Optional[str]) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    day_start = datetime(day.year, day.month, day.day, tzinfo=WARSAW_TZ).astimezone(timezone.utc)
    nxt = day + timedelta(days=1)
    day_end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=WARSAW_TZ).astimezone(timezone.utc)
    spans = []
    for f in frames or []:
        st = _p(f.get("start")) if isinstance(f, dict) else None
        if st is None:
            continue
        spans.append((st, _p(f.get("end")) or st + timedelta(hours=1)))
    if not spans:
        return False
    spans.sort()
    if spans[0][0] > day_start or max(e for _, e in spans) < day_end:
        return False
    missing = timedelta(0)
    reach = spans[0][1]
    for st, en in spans[1:]:
        if st > reach:
            missing += st - reach
        reach = max(reach, en)
    return missing <= timedelta(hours=energy_defaults.PRICE_COVERAGE_MAX_MISSING_HOURS)


def cache_is_reusable(cached: Dict[str, Any], sell_target_hours: Any, ev_target_hours: Any) -> bool:
    """
    A cached payload is reusable only if it was built for the current Europe/Warsaw date (so 00:00:30 never reuses
    yesterday's "today" windows), with the same target hours (legacy caches without hours: defaults) and while its
    rolling-plan horizon (horizon_end, the end of its published prices) still lies ahead.
    """
    horizon = _iso_dt(cached.get("horizon_end")) if cached.get("horizon_end") else None
    return (
        cached.get("_warsaw_date") == datetime.now(WARSAW_TZ).date().isoformat()
        and (horizon is None or horizon.tzinfo is None or horizon > datetime.now(WARSAW_TZ))
        and cached.get("_sell_target_hours", energy_defaults.SELL_TARGET_HOURS) == sell_target_hours
        and cached.get("_ev_target_hours", "auto") == ev_target_hours
    )


def _pricing_frame_ok(f: Any) -> bool:
    p = ((f.get("metrics") or {}).get("pricing") or {}) if isinstance(f, dict) else {}
    return bool(isinstance(f, dict) and f.get("start") and (p.get("price_gross") is not None or p.get("price_net") is not None))


def _cached_active_slots() -> Optional[List[Dict[str, Any]]]:
    """The inverter table last cached (same source the sync's telemetry read reports), None when unknown."""
    if solarman_v5_client is None:
        return None
    try:
        at = (solarman_v5_client.load_deye_cache(ignore_ttl=True) or {}).get("active_tou")
    except Exception:
        return None
    return at.get("slots") if isinstance(at, dict) and at.get("enabled", True) else None


def rolling_plan_fields(
    payload: Dict[str, Any], *, current_soc: float, min_soc: float, max_soc: float, capacity_kwh: float, voltage: float,
    loss_pct: float, calibration_active: bool, max_discharge_current: Optional[float],
    active_slots: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """plan_timeline / deye_tou_schedule / horizon_end of a consolidated payload: one plan_rolling_tou call on its
    pricing_frames from now with the household load profile (the same call the inverter sync makes)."""
    plan = plan_rolling_tou(
        payload_pricing_frames(payload), datetime.now(WARSAW_TZ), current_soc, load_household_profile(),
        min_soc=min_soc, max_soc=max_soc, capacity_kwh=capacity_kwh, voltage=voltage,
        ceiling_a=energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A, loss_pct=loss_pct,
        calibration_active=calibration_active, max_discharge_current=max_discharge_current,
        active_slots=active_slots if active_slots is not None else _cached_active_slots(),
    ) or {}
    return {"plan_timeline": plan.get("plan_timeline", []), "deye_tou_schedule": plan.get("deye_tou_schedule", []),
            "horizon_end": plan.get("horizon_end")}


def fetch_consolidated_data(
    api_key: Optional[str] = None,
    installation: str = "dol",
    hours_ahead: int = 48,
    use_cache: bool = True,
    cache_ttl: int = CACHE_TTL_SECONDS,
    cache_dir: str = DEFAULT_CACHE_DIR,
    max_soc: Optional[int] = None,
    min_soc: Optional[int] = None,
    current_soc: Optional[int] = None,
    calibration_active: bool = False,
    max_charge_current: Optional[float] = None,
    battery_capacity_kwh: Optional[float] = None,
    nominal_voltage: Optional[float] = None,
    loss_buffer_pct: Optional[float] = None,
    sell_target_hours: Any = None,
    ev_target_hours: Any = "auto",
) -> Dict[str, Any]:
    """
    Fetches all 5 datasets (latest, forward 48h, today, month, year),
    applies smart 15-min file caching in /tmp/pstryk_cache_{installation}.json,
    computes cheapest window, and returns full consolidated payload backward-compatible
    with existing Home Assistant sensors.
    """
    if not api_key or api_key == DEFAULT_API_KEY:
        resolved_k, _ = resolve_api_key(installation=installation)
        if resolved_k:
            api_key = resolved_k
        else:
            api_key = DEFAULT_API_KEY

    cache_file = get_cache_file_path(installation, cache_dir=cache_dir)
    sell_target_hours = parse_target_hours(sell_target_hours, energy_defaults.SELL_TARGET_HOURS)
    ev_target_hours = parse_target_hours(ev_target_hours, "auto")

    # 1. Smart cache lookup
    if use_cache:
        cached = read_cache(cache_file, ttl_seconds=cache_ttl)
        if (
            cached is not None
            and isinstance(cached, dict)
            and "current" in cached
            and "today" in cached
            and "all_prices" in cached
            and cache_is_reusable(cached, sell_target_hours, ev_target_hours)
        ):
            # Resolve latest dispatch parameters (zero network overhead, cache-first)
            (
                c_max_soc,
                c_min_soc,
                c_shutdown_soc,
                c_current_soc,
                c_loss_buffer_pct,
            ) = resolve_battery_dispatch_parameters(
                max_soc=max_soc,
                min_soc=min_soc,
                current_soc=current_soc,
                loss_buffer_pct=loss_buffer_pct,
            )
            c_capacity, c_voltage, c_charge_curr, c_discharge_curr = resolve_battery_hardware_parameters(
                battery_capacity_kwh=battery_capacity_kwh,
                nominal_voltage=nominal_voltage,
                max_charge_current=max_charge_current,
            )

            # Re-plan with the latest dispatch settings: the same single planner call as a miss and the sync
            cached.update(rolling_plan_fields(
                cached, current_soc=c_current_soc, min_soc=c_min_soc, max_soc=c_max_soc, capacity_kwh=c_capacity,
                voltage=c_voltage, loss_pct=c_loss_buffer_pct, calibration_active=calibration_active,
                max_discharge_current=c_discharge_curr))

            # Re-evaluate battery model
            b_model = BatteryModel(
                capacity_kwh=c_capacity,
                max_soc=float(c_max_soc),
                min_soc=float(c_min_soc),
                shutdown_soc=float(c_shutdown_soc),
                charge_current_a=float(c_charge_curr),
                discharge_current_a=float(c_discharge_curr),
                voltage_v=c_voltage,
            )
            cached["battery_model"] = b_model.to_dict()

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
    # "Today" is the Europe/Warsaw calendar day (Warsaw 00:00 is 22:00/23:00 UTC of the previous day)
    midnight_utc = datetime.now(WARSAW_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
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
    if isinstance(latest_data, dict) and latest_data.get("status_code") in (400, 401, 403):
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
                "average_price": 0.0,
                "display": "Brak danych",
                "all_prices": [],
                "net": {"min": 0.0, "max": 0.0, "avg": 0.0},
                "gross": {"min": 0.0, "max": 0.0, "avg": 0.0},
            }

    # 5. Extract and partition pricing frames by Warsaw calendar day
    today_frames = today_data.get("frames", []) if (isinstance(today_data, dict) and today_data.get("frames")) else []
    forward_frames = forward_data.get("frames", []) if (isinstance(forward_data, dict) and forward_data.get("frames")) else []

    all_frames_dict = {}
    for f in (today_frames + forward_frames):
        if isinstance(f, dict) and f.get("start"):
            all_frames_dict[f["start"]] = f
    all_frames = sorted(all_frames_dict.values(), key=lambda x: x.get("start", ""))

    now_warsaw = datetime.now(WARSAW_TZ)
    today_date = now_warsaw.date()
    tomorrow_date = (now_warsaw + timedelta(days=1)).date()

    def get_frame_warsaw_date(frame):
        s = frame.get("start")
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(WARSAW_TZ).date()
        except Exception:
            return None

    frames_today = [f for f in all_frames if get_frame_warsaw_date(f) == today_date]

    def _pricing_only(f: Dict[str, Any]) -> Dict[str, Any]:
        return {"start": f.get("start"), "end": f.get("end"), "metrics": {"pricing": (f.get("metrics") or {}).get("pricing")}}
    frames_tomorrow = [f for f in all_frames if get_frame_warsaw_date(f) == tomorrow_date]
    # Require full-day coverage (23/24/25 h across DST) before deriving TOU dispatch from a day's prices
    today_complete = warsaw_day_covered(frames_today, today_date)
    has_tomorrow_pricing = warsaw_day_covered(frames_tomorrow, tomorrow_date)

    # Resolve effective SOC parameters
    (
        eff_max_soc,
        eff_min_soc,
        eff_shutdown_soc,
        eff_current_soc,
        eff_loss_buffer_pct,
    ) = resolve_battery_dispatch_parameters(
        max_soc=max_soc,
        min_soc=min_soc,
        current_soc=current_soc,
        loss_buffer_pct=loss_buffer_pct,
    )
    eff_capacity, eff_voltage, eff_charge_curr, eff_discharge_curr = resolve_battery_hardware_parameters(
        battery_capacity_kwh=battery_capacity_kwh,
        nominal_voltage=nominal_voltage,
        max_charge_current=max_charge_current,
    )

    # Dispatch windows for TODAY (stable across all 24h of the current day)
    target_today_data = {"frames": frames_today} if frames_today else (forward_data if (isinstance(forward_data, dict) and forward_data.get("frames")) else today_data)
    ev_window_today = find_ev_best_window(target_today_data, target_hours=ev_target_hours, now_dt=now_warsaw)
    charge_need_kw = dict(
        current_soc=eff_current_soc, max_soc=eff_max_soc, capacity_kwh=eff_capacity, voltage=eff_voltage,
        ceiling_a=energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A, loss_pct=eff_loss_buffer_pct,
    )
    pb_window_today = find_powerbank_best_window(target_today_data, target_hours="auto", allow_disjoint=False, now_dt=now_warsaw, **charge_need_kw)
    discharge_windows_today = find_best_discharge_windows(target_today_data)
    sell_window_today = find_best_sell_window(target_today_data, target_hours=sell_target_hours)
    tou_error = None
    if not today_complete:
        tou_error = (
            f"Niepełne ceny Pstryk na dziś ({len(frames_today)} h, luki w danych) — "
            "nie wyliczono harmonogramu TOU (zachowano dotychczasowy)."
        )
    if has_tomorrow_pricing:
        pb_window_tomorrow = find_powerbank_best_window(
            {"frames": frames_tomorrow}, target_hours="auto", allow_disjoint=False, now_dt=now_warsaw + timedelta(days=1),
            **dict(charge_need_kw, current_soc=eff_max_soc))  # sized like the rolling planner sizes a later price day
    else:
        pb_window_tomorrow = None
    # Dispatch windows for TOMORROW (published ~12:00 Warsaw time)
    if has_tomorrow_pricing:
        target_tomorrow_data = {"frames": frames_tomorrow}
        tomorrow_ref_dt = now_warsaw + timedelta(days=1)
        ev_window_tomorrow = find_ev_best_window(target_tomorrow_data, target_hours=ev_target_hours, now_dt=tomorrow_ref_dt)
        discharge_windows_tomorrow = find_best_discharge_windows(target_tomorrow_data)
        sell_window_tomorrow = find_best_sell_window(target_tomorrow_data, target_hours=sell_target_hours)
    else:
        pending_msg = "Oczekiwanie na publikację (ok. 12:00)"
        ev_window_tomorrow = {
            "start": None, "end": None, "duration_hours": 0, "average_price": 0.0,
            "display": pending_msg, "all_prices": [],
        }
        pb_window_tomorrow = {
            "start": None, "end": None, "duration_hours": 0, "average_price": 0.0,
            "display": pending_msg, "slots": [], "is_consecutive": True,
        }
        sell_window_tomorrow = {
            "start": None, "end": None, "duration_hours": 0, "average_price": 0.0,
            "display": pending_msg, "spike_hour": None, "spike_price": None,
        }
        discharge_windows_tomorrow = {"error": pending_msg, "display": pending_msg, "peaks": []}

    # Battery model dynamically adopting capacity, voltage, and current settings
    battery_model = BatteryModel(
        capacity_kwh=eff_capacity,
        max_soc=float(eff_max_soc),
        min_soc=float(eff_min_soc),
        shutdown_soc=float(eff_shutdown_soc),
        charge_current_a=float(eff_charge_curr),
        discharge_current_a=float(eff_discharge_curr),
        voltage_v=eff_voltage,
    )

    # 6. Build rich structured sub-objects
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
        "average_price": window_result.get("average_price", 0.0),
        "display": window_result.get("display", "Brak danych"),
        "all_prices": window_result.get("all_prices", []),
        "net": window_result.get("net", {"min": 0.0, "max": 0.0, "avg": 0.0}),
        "gross": window_result.get("gross", {"min": 0.0, "max": 0.0, "avg": 0.0}),
        "ev_best_window": ev_window_today,
        "powerbank_best_window": pb_window_today,
        "best_sell_window": sell_window_today,
        "best_discharge_windows": discharge_windows_today,
        "ev_best_window_today": ev_window_today,
        "powerbank_best_window_today": pb_window_today,
        "best_sell_window_today": sell_window_today,
        "ev_best_window_tomorrow": ev_window_tomorrow,
        "powerbank_best_window_tomorrow": pb_window_tomorrow,
        "best_sell_window_tomorrow": sell_window_tomorrow,
        "best_discharge_windows_tomorrow": discharge_windows_tomorrow,
        # Priced frames (pricing only, now -> end of the published prices) so cache hits and the sync path run the
        # same rolling planner on the same prices
        "pricing_frames": [_pricing_only(f) for f in all_frames if _pricing_frame_ok(f)],
        "has_tomorrow_pricing": has_tomorrow_pricing,
        "price_coverage_complete": today_complete,
        "_warsaw_date": today_date.isoformat(),
        "_sell_target_hours": sell_target_hours,
        "_ev_target_hours": ev_target_hours,
        "battery_model": battery_model.to_dict(),
        "installation": installation,
        "current": current_obj,
        "today": today_obj,
        "month": month_obj,
        "year": year_obj,
        "_cached_at": time.time(),
        "frames": forward_data.get("frames", []),
    }

    if tou_error:
        consolidated.update(plan_timeline=[], deye_tou_schedule=[], horizon_end=None)
    else:
        consolidated.update(rolling_plan_fields(
            consolidated, current_soc=eff_current_soc, min_soc=eff_min_soc, max_soc=eff_max_soc,
            capacity_kwh=eff_capacity, voltage=eff_voltage, loss_pct=eff_loss_buffer_pct,
            calibration_active=calibration_active, max_discharge_current=eff_discharge_curr))
    if tou_error:
        consolidated["tou_error"] = tou_error
    elif use_cache:
        # Degraded (incomplete-coverage) payloads are never cached, so the next run retries the fetch
        write_cache(cache_file, consolidated)

    return consolidated


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Pstryk Dynamic Energy Engine")
    parser.add_argument("--key", help="Pstryk API Key")
    parser.add_argument("--hours", type=int, default=energy_defaults.DEFAULT_HOURS_AHEAD, help="Look-ahead window in hours")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    parser.add_argument("--installation", choices=["dol", "gora"], default=energy_defaults.DEFAULT_INSTALLATION, help="Target installation")
    parser.add_argument("--no-cache", action="store_true", help="Bypass file cache")
    parser.add_argument("--sync-deye-tou", action="store_true", help="Recalculate and synchronize 6-slot TOU schedule to Deye inverter")
    parser.add_argument("--inverter-host", default=None, help="Deye Inverter logger IP (defaults to configured IP)")
    parser.add_argument("--inverter-port", type=int, default=None, help="Deye Inverter logger Port (defaults to configured port)")
    parser.add_argument("--current-soc", type=int, default=None, help="Battery SOC override")
    parser.add_argument("--max-soc", type=int, default=None, help="Max Charge SOC override")
    parser.add_argument("--min-soc", type=int, default=None, help="Min Discharge SOC override")
    parser.add_argument("--shutdown-soc", type=int, default=None, help="Shutdown SOC override (Reg 111)")
    parser.add_argument("--apply-battery-safety-limits", action="store_true", help="Apply confirmed battery safety limits (Reg 110 Min SOC & Reg 111 Shutdown SOC) to Deye inverter")
    parser.add_argument("--calibration", action="store_true", help="Flag to charge to 100 percent for BMS calibration")
    parser.add_argument("--print-tou", action="store_true", help="Print the rolling plan timeline and the 6-slot TOU table (24 h from now) without writing to the inverter")
    parser.add_argument("--force", action="store_true", help="Force TOU write even if schedule is identical to current inverter settings")
    parser.add_argument("--max-charge-current", type=float, default=None, help="Inverter max charge current override (Amperes)")
    parser.add_argument("--max-discharge-current", type=float, default=None, help="Inverter max discharge current override (Amperes)")
    parser.add_argument("--set-charge-current", type=int, default=None, help="Modulate Max Charge Current (Reg 108 in Amperes)")
    parser.add_argument("--restore-charge-current", action="store_true", help="Restore Max Charge Current (Reg 108) to baseline")
    parser.add_argument("--battery-capacity", type=float, default=None, help="Battery total capacity in kWh (dynamically resolved from Deye/env if omitted)")
    parser.add_argument("--nominal-voltage", type=float, default=None, help="Nominal battery pack voltage in Volts (dynamically resolved from Deye/env if omitted)")
    parser.add_argument("--sell-target-hours", default=str(energy_defaults.SELL_TARGET_HOURS), help='Battery sell/discharge window length in hours (int or HA value like "3h")')
    parser.add_argument("--ev-target-hours", default="auto", help='EV charging window hours: "auto" (2h weekday / 4h weekend), int, or HA value like "3h" / "Auto 2h/4h"')
    parser.add_argument("--loss-buffer-pct", type=float, default=energy_defaults.DEFAULT_LOSS_BUFFER_PCT, help="Charging energy loss buffer percentage")

    args = parser.parse_args(argv)
    sell_hours = parse_target_hours(args.sell_target_hours, energy_defaults.SELL_TARGET_HOURS)
    ev_hours = parse_target_hours(args.ev_target_hours, "auto")

    if args.apply_battery_safety_limits:
        try:
            import solarman_v5_client
            res = solarman_v5_client.apply_confirmed_battery_safety_limits(
                host=args.inverter_host,
                port=args.inverter_port,
                min_soc=args.min_soc,
                shutdown_soc=args.shutdown_soc,
            )
        except Exception as e:
            res = {"success": False, "error": str(e)}
        print(json.dumps(res, indent=2))
        return 0

    if args.set_charge_current is not None or args.restore_charge_current:
        target_a = args.set_charge_current if args.set_charge_current is not None else int(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A)
        res = set_charge_current_with_fallback(args.inverter_host, args.inverter_port, target_a)
        print(json.dumps(res, indent=2))
        return 0 if res.get("success") else 1

    resolved_key, key_source = resolve_api_key(args.installation, args.key)
    api_key = resolved_key or DEFAULT_API_KEY

    if args.print_tou:
        data = fetch_consolidated_data(
            api_key=api_key,
            installation=args.installation,
            hours_ahead=args.hours,
            use_cache=not args.no_cache,
            max_soc=args.max_soc,
            min_soc=args.min_soc,
            current_soc=args.current_soc,
            calibration_active=args.calibration,
            max_charge_current=args.max_charge_current,
            battery_capacity_kwh=args.battery_capacity,
            nominal_voltage=args.nominal_voltage,
            loss_buffer_pct=args.loss_buffer_pct,
            sell_target_hours=sell_hours,
            ev_target_hours=ev_hours,
        )
        if not isinstance(data, dict) or "error" in data:
            print(f"Błąd pobierania danych: {data.get('error') if isinstance(data, dict) else data}")
            return 1

        slots = data.get("deye_tou_schedule", [])
        print(f"\n=== PLAN TOU (TERAZ -> {format_hhmm(data.get('horizon_end'))}) ===")
        for e in data.get("plan_timeline") or []:
            print(f"{format_hhmm(e['start'])}-{format_hhmm(e['end'])} {e['role']:<6} SOC>={e['target_soc']:<3} "
                  f"proj {e['soc_start']:>5}->{e['soc_end']:<5} {e['charge_a']:>3} A {e['price']:.3f} zł")
        print("\n=== TABELA W FALOWNIKU (24 h) ===")
        if not slots:
            print("Brak harmonogramu (brak cen Pstryk).")
            return 0

        header = f"{'Slot':<5} | {'Godzina':<7} | {'Moc (W)':<8} | {'Prąd (~A)':<9} | {'SOC (%)':<8} | {'Sieć':<6} | {'Tryb Pracy'}"
        print(header)
        print("-" * len(header))
        _, eff_v, _, _ = resolve_battery_hardware_parameters(nominal_voltage=args.nominal_voltage)
        for s in slots:
            slot_num = s.get("slot", 0)
            time_str = s.get("time", "--:--")
            pwr = s.get("display_power_w") if s.get("display_power_w") is not None else s.get("power_w", energy_defaults.DEFAULT_TRANSFER_POWER_W)
            approx_a = s.get("display_current_a") if s.get("display_current_a") is not None else round(pwr / eff_v)
            soc = s.get("target_soc", 0)
            grid = "TAK" if s.get("grid_charge") else "NIE"
            label = s.get("label", "")
            print(f"{slot_num:<5} | {time_str:<7} | {pwr:<8} | ~{approx_a:<8} | {soc:<8} | {grid:<6} | {label}")
        return 0

    if args.sync_deye_tou:
        sync_res = sync_deye_inverter_tou_schedule(
            host=args.inverter_host,
            port=args.inverter_port,
            current_soc=args.current_soc,
            max_soc=args.max_soc,
            min_soc=args.min_soc,
            shutdown_soc=args.shutdown_soc,
            calibration_active=args.calibration,
            api_key=resolved_key,
            installation=args.installation,
            force=args.force,
            max_charge_current=args.max_charge_current,
            max_discharge_current=args.max_discharge_current,
            battery_capacity_kwh=args.battery_capacity,
            nominal_voltage=args.nominal_voltage,
            loss_buffer_pct=args.loss_buffer_pct,
            sell_target_hours=sell_hours,
            ev_target_hours=ev_hours,
        )
        print(json.dumps(sync_res, indent=2))
        return 0 if (sync_res.get("success") or sync_res.get("unchanged")) else 1

    debug_info = {}
    if not resolved_key:
        if args.key and args.key.startswith("!secret"):
            debug_info["key_status"] = "Not Substituted"
        else:
            debug_info["key_status"] = "Missing"
    else:
        debug_info["key_status"] = "Present"
    debug_info["key_source"] = key_source

    if not resolved_key:
        err_res = {"error": f"API Key Issue: {debug_info['key_status']}", "debug": debug_info}
        print(json.dumps(err_res, indent=2))
        return 1

    result = fetch_consolidated_data(
        api_key=api_key,
        installation=args.installation,
        hours_ahead=args.hours,
        use_cache=not args.no_cache,
        max_soc=args.max_soc,
        min_soc=args.min_soc,
        current_soc=args.current_soc,
        calibration_active=args.calibration,
        max_charge_current=args.max_charge_current,
        battery_capacity_kwh=args.battery_capacity,
        nominal_voltage=args.nominal_voltage,
        loss_buffer_pct=args.loss_buffer_pct,
        sell_target_hours=sell_hours,
        ev_target_hours=ev_hours,
    )

    if isinstance(result, dict):
        result["debug"] = debug_info
        result["installation"] = args.installation

    print(json.dumps(result, indent=2))
    return 0 if (isinstance(result, dict) and "error" not in result) else 1


if __name__ == "__main__":
    sys.exit(main())
