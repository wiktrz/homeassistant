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
        repo_root = os.path.abspath(os.path.join(script_dir, ".."))
        paths = [
            "/config/.env",
            os.path.join(repo_root, ".env"),
            os.path.join(script_dir, ".env"),
            os.path.join(os.getcwd(), ".env"),
        ]
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
    pre_peak_margin: Optional[int] = None,
    loss_buffer_pct: Optional[float] = None,
    inv_cfg: Optional[Dict[str, Any]] = None,
    inv_tlm: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, int, int, int, float]:
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
     eff_pre_peak_margin, eff_loss_buffer_pct)
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
    eff_pre_peak = int(pre_peak_margin) if pre_peak_margin is not None else energy_defaults.DEFAULT_PRE_PEAK_MARGIN
    eff_loss = float(loss_buffer_pct) if loss_buffer_pct is not None else energy_defaults.DEFAULT_LOSS_BUFFER_PCT

    return (
        safe_max_soc,
        safe_min_soc,
        safe_shutdown_soc,
        safe_current_soc,
        eff_pre_peak,
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
    pre_peak_margin: Optional[int] = None,
    nominal_voltage: Optional[float] = None,
    loss_buffer_pct: Optional[float] = None,
    best_discharge_windows: Optional[Any] = None,
    pricing_data: Optional[Dict[str, Any]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Generates the 6-slot Deye Time-of-Use schedule from the day's price structure (no clock heuristics).
    Timeline: the analyse_day_price_profile segments of pricing_data (or, without price frames, the explicitly
    supplied price-derived best_discharge_windows / best_sell_window as peaks), overlaid with the grid-charge window.
    Slot semantics (zero export, peaks ranked by gross retail buy price):
      - charge window -> grid charge to Max SOC; Reg 108 target = clamp(ceiling * min_h / T, floor, ceiling);
      - peak -> autokonsumpcja to Min SOC, or to the pre-peak reserve when a higher-ranked peak follows
        before the next charge;
      - other hours: after a charge until a peak/charge hold Max SOC; after a peak until a charge drain to Min SOC;
        between two peaks hold the level the previous peak left.
    Fitting 6 slots: identical first/last slots merge across midnight; >6 merges the adjacent pair with the smallest
    price difference (charge window and peak starts kept); <6 splits the longest slot on an hour boundary.
    Returns None (nothing to write) when there is no price data: no pricing_data frames, no best_discharge_windows
    and no best_sell_window.
    """
    def to_time_str(m: int) -> str:
        return f"{(m // 60) % 24:02d}:{m % 60:02d}"

    def split_wrap(a: int, b: int) -> List[Tuple[int, int]]:
        """[a, b) in minutes (b <= a wraps past midnight) as intervals inside one day."""
        if b <= a:
            b += 1440
        return [(a, min(b, 1440))] + ([(0, b - 1440)] if b > 1440 else [])

    # 1. Price timeline: price-profile segments, or explicit price-derived peak windows
    priced = priced_day_frames(pricing_data.get("frames")) if isinstance(pricing_data, dict) else []
    if priced:
        timeline = [
            {"kind": s["kind"], "start_m": s["start_m"], "end_m": s["end_m"], "avg": s["avg"]}
            for s in analyse_day_price_profile(priced)["segments"]
        ]
    else:
        source = best_discharge_windows or best_sell_window
        if isinstance(source, dict) and isinstance(source.get("peaks"), list):
            items = source["peaks"]
        else:
            items = source if isinstance(source, list) else [source]
        peak_ivs = []
        for w in items:
            if isinstance(w, dict) and "error" not in w and w.get("start") and w.get("end"):
                a, b = hhmm_to_minutes(format_hhmm(w["start"])), hhmm_to_minutes(format_hhmm(w["end"]))
                if a is not None and b is not None:
                    peak_ivs += [(s, e, w.get("average_price")) for s, e in split_wrap(a, b)]
        if not peak_ivs:
            return None
        timeline, cursor = [], 0
        for a, b, avg in sorted(peak_ivs, key=lambda iv: iv[:2]):
            a = max(a, cursor)
            if b <= a:
                continue
            if a > cursor:
                timeline.append({"kind": "valley", "start_m": cursor, "end_m": a, "avg": None})
            timeline.append({"kind": "peak", "start_m": a, "end_m": b, "avg": avg})
            cursor = b
        if cursor < 1440:
            timeline.append({"kind": "valley", "start_m": cursor, "end_m": 1440, "avg": None})

    # Contiguous [0, 1440): close holes (missing hour, DST) and drop a repeated DST hour
    contiguous: List[Dict[str, Any]] = []
    for seg in timeline:
        if contiguous and seg["end_m"] <= contiguous[-1]["end_m"]:
            continue
        contiguous.append(dict(seg, start_m=contiguous[-1]["end_m"]) if contiguous else seg)
    contiguous[0]["start_m"] = 0
    contiguous[-1]["end_m"] = 1440
    timeline = contiguous

    # 2. Grid-charge window overlay (charge wins over any price segment)
    charge_ivs: List[Tuple[int, int]] = []
    if isinstance(best_pb_window, dict) and "error" not in best_pb_window and best_pb_window.get("start"):
        if best_pb_window.get("is_consecutive", True) is False and best_pb_window.get("slots"):
            bounds = [str(sl).split(" - ") for sl in best_pb_window["slots"]]
        else:
            bounds = [(format_hhmm(best_pb_window.get("start")), format_hhmm(best_pb_window.get("end")))]
        for pair in bounds:
            mins = [hhmm_to_minutes(x.strip()) for x in pair]
            if len(mins) == 2 and None not in mins:
                charge_ivs += split_wrap(mins[0], mins[-1])
    charge_avg = best_pb_window.get("average_price") if isinstance(best_pb_window, dict) else None
    for a, b in charge_ivs:
        cut = []
        for seg in timeline:
            if seg["end_m"] <= a or seg["start_m"] >= b:
                cut.append(seg)
                continue
            if seg["start_m"] < a:
                cut.append(dict(seg, end_m=a))
            if seg["end_m"] > b:
                cut.append(dict(seg, start_m=b))
        cut.append({"kind": "charge", "start_m": a, "end_m": b, "avg": charge_avg})
        timeline = sorted(cut, key=lambda s: s["start_m"])

    # 3. Hardware + dispatch parameters (Rule 9: from inverter reads / .env, never literals)
    eff_cap, eff_volts, eff_charge_curr, eff_discharge_curr = resolve_battery_hardware_parameters(
        battery_capacity_kwh=battery_capacity_kwh,
        nominal_voltage=nominal_voltage,
        max_charge_current=max_charge_current,
        max_discharge_current=max_discharge_current,
    )
    if max_discharge_current is not None and float(max_discharge_current) > 0:
        base_power_w = min(energy_defaults.INVERTER_MAX_POWER_W, max(energy_defaults.INVERTER_MIN_POWER_W, int(round(float(max_discharge_current) * eff_volts, -1))))
    elif default_power_w is not None and int(default_power_w) > 0:
        base_power_w = int(default_power_w)
    elif eff_discharge_curr > 0 and eff_volts > 0:
        base_power_w = min(energy_defaults.INVERTER_MAX_POWER_W, max(energy_defaults.INVERTER_MIN_POWER_W, int(round(eff_discharge_curr * eff_volts, -1))))
    else:
        base_power_w = energy_defaults.DEFAULT_TRANSFER_POWER_W

    safe_max_soc, safe_min_soc, safe_shutdown_soc, eff_current_soc, eff_pre_peak, eff_loss = resolve_battery_dispatch_parameters(
        max_soc=max_soc,
        min_soc=min_soc,
        current_soc=current_soc,
        pre_peak_margin=pre_peak_margin,
        loss_buffer_pct=loss_buffer_pct,
    )
    if calibration_active:
        safe_max_soc = 100
    pre_peak_soc = min(safe_max_soc, safe_min_soc + max(0, int(eff_pre_peak)))

    # Reg 108: always based on the configured ceiling, never on the live register value (no ratchet)
    ceiling_curr = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))
    eff_charge_curr = float(ceiling_curr)
    charge_hours = sum(b - a for a, b in charge_ivs) // 60
    min_hours = charge_window_min_hours(eff_current_soc, safe_max_soc, eff_cap, eff_volts, ceiling_curr, eff_loss)
    if calibration_active:
        target_charge_current_a = ceiling_curr
    else:
        target_charge_current_a = charge_current_for_window(min_hours, charge_hours or min_hours, ceiling_curr)

    charge_power_w = min(energy_defaults.INVERTER_MAX_POWER_W, max(energy_defaults.INVERTER_MIN_POWER_W, int(round(target_charge_current_a * eff_volts, -1))))
    discharge_power_w = base_power_w
    eff_dis_a = int(round(eff_discharge_curr)) if eff_discharge_curr > 0 else (int(round(base_power_w / eff_volts)) if eff_volts > 0 else int(energy_defaults.FALLBACK_BATTERY_MAX_DISCHARGE_CURRENT))

    def _build_slot_dict(slot_num: int, time_m: int, target_soc: int, grid_charge: bool, label: str, role: str) -> Dict[str, Any]:
        is_charge = bool(grid_charge)
        act_curr = target_charge_current_a if is_charge else eff_dis_a
        act_pwr = charge_power_w if is_charge else discharge_power_w
        return {
            "slot": slot_num,
            "time": to_time_str(time_m),
            "power_w": discharge_power_w,
            "target_soc": target_soc,
            "grid_charge": is_charge,
            "charge_current_a": target_charge_current_a if is_charge else 0,
            "charge_power_w": charge_power_w if is_charge else 0,
            "discharge_current_a": eff_dis_a,
            "discharge_power_w": discharge_power_w,
            "active_current_a": act_curr,
            "active_power_w": act_pwr,
            "display_current_a": act_curr,
            "display_power_w": act_pwr,
            "target_charge_current_a": target_charge_current_a,
            "base_charge_current_a": int(round(eff_charge_curr)),
            "label": label,
            "role": role,
            "voltage": eff_volts,
        }

    # 4. Behaviour per segment: (target_soc, grid_charge, label, role)
    def _assign(tl: List[Dict[str, Any]]) -> List[Tuple[int, bool, str, str]]:
        n = len(tl)
        kinds = [s["kind"] for s in tl]
        peak_order = [i for i in range(n) if kinds[i] == "peak"]
        charge_order = [i for i in range(n) if kinds[i] == "charge"]
        span = lambda s: f"({to_time_str(s['start_m'])}-{to_time_str(s['end_m'])})"

        def peak_soc(i: int) -> int:
            for j in ((i + k) % n for k in range(1, n)):
                if kinds[j] == "charge":
                    break
                if kinds[j] == "peak" and None not in (tl[i]["avg"], tl[j]["avg"]) and tl[j]["avg"] > tl[i]["avg"]:
                    return pre_peak_soc
            return safe_min_soc

        out: List[Tuple[int, bool, str, str]] = []
        for i, seg in enumerate(tl):
            if seg["kind"] == "charge":
                name = "Najtańsze Ładowanie" if i == charge_order[0] else "Tania Godzina (Doładowanie)"
                label = "Ładowanie (Kalibracja 100%)" if calibration_active else f"{name} {span(seg)}"
                out.append((safe_max_soc, True, label, "charge"))
            elif seg["kind"] == "peak":
                # UI name by order only: first / last of several peaks, any other (or a lone) peak is neutral
                is_first, is_last = i == peak_order[0], i == peak_order[-1]
                name = "Szczyt Cenowy" if is_first == is_last else ("Szczyt Poranny" if is_first else "Szczyt Wieczorny")
                soc = peak_soc(i)
                mode = "Autokonsumpcja" if soc == safe_min_soc else "Autokonsumpcja do Rezerwy"
                out.append((soc, False, f"{name} ({mode}) {span(seg)}", "peak"))
            else:
                bounds_before = [j for j in ((i - k) % n for k in range(1, n)) if kinds[j] != "valley"]
                bounds_after = [j for j in ((i + k) % n for k in range(1, n)) if kinds[j] != "valley"]
                head = not any(j < i for j in bounds_before)
                last = not any(j > i for j in bounds_after)
                prev_k = kinds[bounds_before[0]] if bounds_before else None
                next_k = kinds[bounds_after[0]] if bounds_after else None
                if prev_k == "charge":
                    hold = "Ochrona Rezerwy (Hold do Szczytu)" if head else "Czuwanie po Ładowaniu (Hold do Szczytu)"
                    out.append((safe_max_soc, False, hold if next_k == "peak" else "Ochrona Rezerwy (Hold)", "reserve"))
                elif prev_k == "peak" and next_k == "charge":
                    drain = "Czuwanie Nocne" if head else "Autokonsumpcja do Min SOC (przed Ładowaniem)"
                    out.append((safe_min_soc, False, drain, "other"))
                elif prev_k == "peak":
                    prev_soc = peak_soc(bounds_before[0])
                    label = "Czuwanie Nocne" if last and prev_soc == safe_min_soc else "Ochrona Rezerwy (Hold)"
                    out.append((prev_soc, False, label, "reserve"))
                else:
                    out.append((safe_min_soc, False, "Czuwanie Nocne", "other"))
        return out

    # 5. Fit exactly TOU_SLOT_COUNT slots
    slot_count = energy_defaults.TOU_SLOT_COUNT
    if len(timeline) > slot_count:
        first_soc, first_grid, _, first_role = _assign(timeline)[0]
        last_soc, last_grid, _, last_role = _assign(timeline)[-1]
        if (first_soc, first_grid, first_role) == (last_soc, last_grid, last_role):
            # identical slots on both sides of midnight: the last slot simply runs on through 00:00
            timeline = timeline[1:]
    while len(timeline) > slot_count:
        kinds = [s["kind"] for s in timeline]
        idx = range(1, len(timeline))
        merge = (
            [j for j in idx if "charge" not in kinds[j - 1 : j + 1] and not (kinds[j] == "peak" and kinds[j - 1] != "peak")]
            or [j for j in idx if "charge" not in kinds[j - 1 : j + 1]]
            or [j for j in idx if kinds[j] != "charge"]
        )

        def price_gap(j: int) -> Tuple[float, int, int]:
            a, b = timeline[j - 1]["avg"], timeline[j]["avg"]
            gap = abs(a - b) if a is not None and b is not None else 0.0
            return gap, timeline[j]["end_m"] - timeline[j]["start_m"], j

        j = min(merge, key=price_gap)
        prev_seg, cur_seg = timeline[j - 1], timeline[j]
        if prev_seg["avg"] is not None and cur_seg["avg"] is not None:
            w_prev, w_cur = prev_seg["end_m"] - prev_seg["start_m"], cur_seg["end_m"] - cur_seg["start_m"]
            merged_avg = round((prev_seg["avg"] * w_prev + cur_seg["avg"] * w_cur) / (w_prev + w_cur), 4)
        else:
            merged_avg = prev_seg["avg"] if prev_seg["avg"] is not None else cur_seg["avg"]
        timeline[j - 1] = dict(prev_seg, end_m=cur_seg["end_m"], avg=merged_avg)
        del timeline[j]

    behaviour = _assign(timeline)
    slots_m = [[seg["start_m"], beh] for seg, beh in zip(timeline, behaviour)]
    while len(slots_m) < slot_count:
        # split the longest slot (non-charge first) at its middle hour boundary; the copy keeps the same behaviour
        lengths = [((slots_m[(k + 1) % len(slots_m)][0] - slots_m[k][0]) % 1440) or 1440 for k in range(len(slots_m))]
        k = min(range(len(slots_m)), key=lambda x: (slots_m[x][1][1], -lengths[x], x))
        mid_h = statistics.median_low(range(1, lengths[k] // 60)) if lengths[k] > 60 else 0
        slots_m.append([(slots_m[k][0] + 60 * mid_h) % 1440, slots_m[k][1]])
        slots_m.sort(key=lambda sm: sm[0])

    return [_build_slot_dict(n + 1, m, *beh) for n, (m, beh) in enumerate(slots_m)]


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


def are_tou_slots_identical(new_slots: List[Dict[str, Any]], active_slots: List[Dict[str, Any]]) -> bool:
    """
    Compares newly calculated 6-slot TOU schedule with active inverter slots.
    Returns True only if all 6 slots match in time, target_soc, grid_charge,
    and power_w (within +-50W tolerance).
    """
    if not isinstance(new_slots, list) or not isinstance(active_slots, list):
        return False
    if len(new_slots) != 6 or len(active_slots) != 6:
        return False

    for ns, aslot in zip(new_slots, active_slots):
        if not isinstance(ns, dict) or not isinstance(aslot, dict):
            return False
        # Normalize and compare time (e.g. "13:00" vs "13:00")
        t_new = str(ns.get("time", "")).strip()
        t_act = str(aslot.get("time", "")).strip()
        if t_new != t_act:
            return False
        # Compare target_soc
        try:
            if int(ns.get("target_soc", 0)) != int(aslot.get("target_soc", 0)):
                return False
        except (ValueError, TypeError):
            return False
        # Compare grid_charge
        if bool(ns.get("grid_charge")) != bool(aslot.get("grid_charge")):
            return False
        # Compare power_w with tolerance
        try:
            p_new = int(ns.get("power_w", energy_defaults.DEFAULT_TRANSFER_POWER_W))
            p_act = int(aslot.get("power_w", energy_defaults.DEFAULT_TRANSFER_POWER_W))
            if abs(p_new - p_act) > energy_defaults.TOU_POWER_DIFF_TOLERANCE_W:
                return False
        except (ValueError, TypeError):
            return False

    return True


def check_and_apply_charge_current_modulation(
    slots: List[Dict[str, Any]],
    host: Optional[str] = None,
    port: Optional[int] = None,
    base_charge_current: Optional[float] = None,
    next_cycle_target_a: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Sets Register 108 (Max A Charge) for the current Warsaw time from today's TOU grid-charge slots:
    - Inside a charge window: that slot's target_charge_current_a (ceiling * min_h / T from the energy need).
    - Before today's first charge window: its target is pre-set.
    - Once today's last charge window has ended: next_cycle_target_a (tomorrow's charge target), if known.
    - Otherwise: the BATTERY_CHARGE_CURRENT_MAX_CEILING_A baseline.
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
    if active is not None:
        desired_a = active[-1]
    elif windows and now_m < min(w[0] for w in windows):
        desired_a = min(windows)[-1]
    elif windows and now_m >= max(w[1] for w in windows) and next_cycle_target_a is not None:
        desired_a = int(next_cycle_target_a)
    else:
        desired_a = ceiling_curr

    desired_a = max(floor_curr, min(ceiling_curr, desired_a))

    mod_res = set_charge_current_with_fallback(host, port, desired_a)

    mod_res["in_charging_window"] = active is not None
    mod_res["desired_a"] = desired_a
    return mod_res


def warsaw_day_pricing(payload: Any, for_tomorrow: bool = False) -> Optional[Dict[str, Any]]:
    """Pricing frames of today's (or tomorrow's) Europe/Warsaw day from a consolidated payload or its cache."""
    if not isinstance(payload, dict):
        return None
    frames = payload.get("pricing_frames_tomorrow" if for_tomorrow else "pricing_frames_today")
    if not frames:
        day = (datetime.now(WARSAW_TZ) + timedelta(days=1 if for_tomorrow else 0)).date()

        def _day(f: Any) -> Any:
            try:
                return datetime.fromisoformat(str(f.get("start")).replace("Z", "+00:00")).astimezone(WARSAW_TZ).date()
            except Exception:
                return None
        frames = [f for f in payload.get("frames") or [] if isinstance(f, dict) and _day(f) == day]
    return {"frames": frames} if frames else None


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
    for_tomorrow: bool = False,
    force: bool = False,
    max_charge_current: Optional[float] = None,
    max_discharge_current: Optional[float] = None,
    battery_capacity_kwh: Optional[float] = None,
    nominal_voltage: Optional[float] = None,
    pre_peak_margin: Optional[int] = None,
    loss_buffer_pct: Optional[float] = None,
    sell_target_hours: Any = None,
    ev_target_hours: Any = "auto",
) -> Dict[str, Any]:
    """
    Recalculates and uploads the 6-slot TOU schedule to the Deye hybrid inverter via Solarman V5 client.
    Reads live inverter telemetry and config when available, and derives optimal windows from Pstryk pricing data.
    Applies idempotent diff check: if calculated slots match the active inverter schedule, write is skipped
    to protect flash memory (EEPROM) and avoid cloud rate limits. Use force=True to bypass diff check.
    Also synchronizes Register 108 (Max A Charge) dynamically: reduced during extended charging windows,
    restored to baseline outside charging windows.
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
        host = host or "10.20.2.6"
        port = port or 8899

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
        eff_pre_peak_margin,
        eff_loss_buffer_pct,
    ) = resolve_battery_dispatch_parameters(
        max_soc=max_soc,
        min_soc=min_soc,
        shutdown_soc=shutdown_soc,
        current_soc=current_soc,
        pre_peak_margin=pre_peak_margin,
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

    # Resolve pricing windows (from cache or API)
    cache_file = get_cache_file_path(installation, cache_dir)
    sell_target_hours = parse_target_hours(sell_target_hours, energy_defaults.SELL_TARGET_HOURS)
    ev_target_hours = parse_target_hours(ev_target_hours, "auto")
    cached_data = read_cache(cache_file)
    if isinstance(cached_data, dict) and not cache_is_reusable(cached_data, sell_target_hours, ev_target_hours):
        cached_data = None
    pb_window = None
    sell_window = None
    discharge_windows = None

    if for_tomorrow:
        has_tomorrow = bool(cached_data and isinstance(cached_data, dict) and cached_data.get("has_tomorrow_pricing"))
        if not has_tomorrow:
            resolved_key, _ = resolve_api_key(installation=installation, explicit_key=api_key)
            if resolved_key:
                try:
                    fresh_data = fetch_consolidated_data(
                        api_key=resolved_key, installation=installation, cache_dir=cache_dir,
                        sell_target_hours=sell_target_hours, ev_target_hours=ev_target_hours,
                    )
                    if isinstance(fresh_data, dict) and fresh_data.get("has_tomorrow_pricing"):
                        cached_data = fresh_data
                        has_tomorrow = True
                except Exception:
                    pass
        if not has_tomorrow:
            err_dict = {
                "success": False,
                "error": "Ceny na jutro nie zostały jeszcze opublikowane przez Pstryk (publikacja ok. 12:00-14:00). Anulowano synchronizację TOU.",
                "for_tomorrow": True,
            }
            save_last_tou_sync_result(err_dict)
            return err_dict

    if cached_data and isinstance(cached_data, dict):
        suffix = "_tomorrow" if for_tomorrow else ""
        pb_window = cached_data.get("powerbank_best_window" + suffix)
        sell_window = cached_data.get("best_sell_window" + suffix)
        discharge_windows = cached_data.get("best_discharge_windows" + suffix)

    source_data = cached_data  # payload the windows came from (also supplies the next-cycle Reg 108 target)
    if not pb_window or not sell_window or (isinstance(pb_window, dict) and "error" in pb_window):
        resolved_key, _ = resolve_api_key(installation=installation, explicit_key=api_key)
        if resolved_key:
            try:
                fresh_data = fetch_consolidated_data(
                    api_key=resolved_key, installation=installation, cache_dir=cache_dir,
                    sell_target_hours=sell_target_hours, ev_target_hours=ev_target_hours,
                )
                if isinstance(fresh_data, dict):
                    source_data = fresh_data
                    if for_tomorrow and fresh_data.get("has_tomorrow_pricing"):
                        pb_window = fresh_data.get("powerbank_best_window_tomorrow")
                        sell_window = fresh_data.get("best_sell_window_tomorrow")
                        discharge_windows = fresh_data.get("best_discharge_windows_tomorrow")
                    elif not for_tomorrow and fresh_data.get("price_coverage_complete") is False:
                        # Never derive/write TOU from a partial price day; keep the inverter's existing schedule
                        pb_window = {"error": fresh_data.get("tou_error") or "Niepełne ceny Pstryk na dziś"}
                        sell_window = None
                    else:
                        pb_window = fresh_data.get("powerbank_best_window")
                        sell_window = fresh_data.get("best_sell_window")
                        discharge_windows = fresh_data.get("best_discharge_windows")
            except Exception:
                pass

    if (
        not pb_window
        or not sell_window
        or (isinstance(pb_window, dict) and "error" in pb_window)
        or (isinstance(sell_window, dict) and "error" in sell_window)
    ):
        err_msg = pb_window.get("error") if (isinstance(pb_window, dict) and "error" in pb_window) else "Brak poprawnych danych cenowych Pstryk"
        err_dict = {
            "success": False,
            "error": f"Nie można zsynchronizować TOU z falownikiem: {err_msg}. Przerwano zapis, aby zachować dotychczasowy harmonogram.",
            "for_tomorrow": for_tomorrow,
        }
        save_last_tou_sync_result(err_dict)
        return err_dict

    # Rule 12: peaks come from the day's gross retail prices (pricing frames / discharge windows), the prosumer
    # sell window is only the last-resort explicit input for payloads without them
    slots = generate_deye_tou_schedule(
        current_soc=eff_current_soc,
        max_soc=eff_max_soc,
        min_soc=eff_min_soc,
        best_pb_window=pb_window,
        best_sell_window=sell_window,
        calibration_active=calibration_active,
        max_charge_current=eff_max_charge_current,
        max_discharge_current=eff_max_discharge_current,
        battery_capacity_kwh=eff_capacity_kwh,
        nominal_voltage=eff_nominal_voltage,
        pre_peak_margin=eff_pre_peak_margin,
        loss_buffer_pct=eff_loss_buffer_pct,
        best_discharge_windows=discharge_windows if isinstance(discharge_windows, dict) and "error" not in discharge_windows else None,
        pricing_data=warsaw_day_pricing(source_data, for_tomorrow),
    )
    if not slots:
        err_dict = {
            "success": False,
            "error": "Nie można zsynchronizować TOU z falownikiem: brak danych cenowych do wyliczenia harmonogramu. Przerwano zapis, aby zachować dotychczasowy harmonogram.",
            "for_tomorrow": for_tomorrow,
        }
        save_last_tou_sync_result(err_dict)
        return err_dict

    # Dynamic Charge Current Modulation check
    charge_mod_res = None
    if not for_tomorrow:
        next_cycle_target_a = None
        if source_data and isinstance(source_data, dict):
            tomorrow_sched = source_data.get("deye_tou_schedule_tomorrow")
            if isinstance(tomorrow_sched, list):
                for ts in tomorrow_sched:
                    if ts.get("grid_charge") and ts.get("target_charge_current_a"):
                        next_cycle_target_a = int(ts["target_charge_current_a"])
                        break

        charge_mod_res = check_and_apply_charge_current_modulation(
            slots=slots,
            host=host,
            port=port,
            next_cycle_target_a=next_cycle_target_a,
        )

    # Idempotent diff check: verify if active inverter slots already match newly calculated schedule
    global _DISPATCH_CACHE
    active_slots = active_tou.get("slots", []) if isinstance(active_tou, dict) else []
    active_enabled = active_tou.get("enabled", True) if isinstance(active_tou, dict) else True
    if not force and active_enabled and are_tou_slots_identical(slots, active_slots):
        _DISPATCH_CACHE = None
        if solarman_v5_client is not None:
            try:
                cached = solarman_v5_client.load_deye_cache(ignore_ttl=True)
                if cached and isinstance(cached, dict):
                    cached["active_tou"] = {"enabled": True, "slots": slots}
                    solarman_v5_client.save_deye_cache(cached)
            except Exception:
                pass
        unchanged_res = {
            "success": True,
            "unchanged": True,
            "message": "Harmonogram TOU w falowniku jest identyczny z wyliczonym — pominięto zapis (ochrona pamięci i limitów API).",
            "timestamp": datetime.now().isoformat(),
            "host": host,
            "port": port,
            "for_tomorrow": for_tomorrow,
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
        "for_tomorrow": for_tomorrow,
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
    yesterday's "today" windows) and with the same target hours (legacy caches without hours: defaults).
    """
    return (
        cached.get("_warsaw_date") == datetime.now(WARSAW_TZ).date().isoformat()
        and cached.get("_sell_target_hours", energy_defaults.SELL_TARGET_HOURS) == sell_target_hours
        and cached.get("_ev_target_hours", "auto") == ev_target_hours
    )


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
    pre_peak_margin: Optional[int] = None,
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
                c_pre_peak_margin,
                c_loss_buffer_pct,
            ) = resolve_battery_dispatch_parameters(
                max_soc=max_soc,
                min_soc=min_soc,
                current_soc=current_soc,
                pre_peak_margin=pre_peak_margin,
                loss_buffer_pct=loss_buffer_pct,
            )
            c_capacity, c_voltage, c_charge_curr, c_discharge_curr = resolve_battery_hardware_parameters(
                battery_capacity_kwh=battery_capacity_kwh,
                nominal_voltage=nominal_voltage,
                max_charge_current=max_charge_current,
            )

            # Re-evaluate TOU schedule today with latest dispatch settings
            pb_w_today = cached.get("powerbank_best_window")
            sell_w_today = cached.get("best_sell_window")
            discharge_w_today = cached.get("best_discharge_windows")
            if pb_w_today and (sell_w_today or discharge_w_today):
                cached["deye_tou_schedule_today"] = generate_deye_tou_schedule(
                    current_soc=c_current_soc,
                    max_soc=c_max_soc,
                    min_soc=c_min_soc,
                    best_pb_window=pb_w_today,
                    best_sell_window=sell_w_today,
                    calibration_active=calibration_active,
                    max_charge_current=c_charge_curr,
                    max_discharge_current=c_discharge_curr,
                    battery_capacity_kwh=c_capacity,
                    nominal_voltage=c_voltage,
                    pre_peak_margin=c_pre_peak_margin,
                    loss_buffer_pct=c_loss_buffer_pct,
                    best_discharge_windows=discharge_w_today,
                    pricing_data=warsaw_day_pricing(cached),
                ) or []
                cached["deye_tou_schedule"] = cached["deye_tou_schedule_today"]

            # Re-evaluate TOU schedule tomorrow if available
            if cached.get("has_tomorrow_pricing"):
                pb_w_tom = cached.get("powerbank_best_window_tomorrow")
                sell_w_tom = cached.get("best_sell_window_tomorrow")
                discharge_w_tom = cached.get("best_discharge_windows_tomorrow")
                if pb_w_tom and (sell_w_tom or discharge_w_tom):
                    cached["deye_tou_schedule_tomorrow"] = generate_deye_tou_schedule(
                        current_soc=c_current_soc,
                        max_soc=c_max_soc,
                        min_soc=c_min_soc,
                        best_pb_window=pb_w_tom,
                        best_sell_window=sell_w_tom,
                        calibration_active=calibration_active,
                        max_charge_current=c_charge_curr,
                        max_discharge_current=c_discharge_curr,
                        battery_capacity_kwh=c_capacity,
                        nominal_voltage=c_voltage,
                        pre_peak_margin=c_pre_peak_margin,
                        loss_buffer_pct=c_loss_buffer_pct,
                        best_discharge_windows=discharge_w_tom,
                        pricing_data=warsaw_day_pricing(cached, for_tomorrow=True),
                    ) or []

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
        eff_pre_peak_margin,
        eff_loss_buffer_pct,
    ) = resolve_battery_dispatch_parameters(
        max_soc=max_soc,
        min_soc=min_soc,
        current_soc=current_soc,
        pre_peak_margin=pre_peak_margin,
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
    tou_today = [] if tou_error else generate_deye_tou_schedule(
        current_soc=eff_current_soc,
        max_soc=eff_max_soc,
        min_soc=eff_min_soc,
        best_pb_window=pb_window_today,
        best_sell_window=sell_window_today,
        calibration_active=calibration_active,
        max_charge_current=eff_charge_curr,
        max_discharge_current=eff_discharge_curr,
        battery_capacity_kwh=eff_capacity,
        nominal_voltage=eff_voltage,
        pre_peak_margin=eff_pre_peak_margin,
        loss_buffer_pct=eff_loss_buffer_pct,
        best_discharge_windows=discharge_windows_today,
        pricing_data=target_today_data,
    ) or []

    # Dispatch windows for TOMORROW (published ~12:00 Warsaw time)
    if has_tomorrow_pricing:
        target_tomorrow_data = {"frames": frames_tomorrow}
        tomorrow_ref_dt = now_warsaw + timedelta(days=1)
        ev_window_tomorrow = find_ev_best_window(target_tomorrow_data, target_hours=ev_target_hours, now_dt=tomorrow_ref_dt)
        pb_window_tomorrow = find_powerbank_best_window(target_tomorrow_data, target_hours="auto", allow_disjoint=False, now_dt=tomorrow_ref_dt, **charge_need_kw)
        discharge_windows_tomorrow = find_best_discharge_windows(target_tomorrow_data)
        sell_window_tomorrow = find_best_sell_window(target_tomorrow_data, target_hours=sell_target_hours)
        tou_tomorrow = generate_deye_tou_schedule(
            current_soc=eff_current_soc,
            max_soc=eff_max_soc,
            min_soc=eff_min_soc,
            best_pb_window=pb_window_tomorrow,
            best_sell_window=sell_window_tomorrow,
            calibration_active=calibration_active,
            max_charge_current=eff_charge_curr,
            max_discharge_current=eff_discharge_curr,
            battery_capacity_kwh=eff_capacity,
            nominal_voltage=eff_voltage,
            pre_peak_margin=eff_pre_peak_margin,
            loss_buffer_pct=eff_loss_buffer_pct,
            best_discharge_windows=discharge_windows_tomorrow,
            pricing_data=target_tomorrow_data,
        ) or []
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
        tou_tomorrow = []

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
        "deye_tou_schedule": tou_today,
        "ev_best_window_today": ev_window_today,
        "powerbank_best_window_today": pb_window_today,
        "best_sell_window_today": sell_window_today,
        "deye_tou_schedule_today": tou_today,
        "ev_best_window_tomorrow": ev_window_tomorrow,
        "powerbank_best_window_tomorrow": pb_window_tomorrow,
        "best_sell_window_tomorrow": sell_window_tomorrow,
        "best_discharge_windows_tomorrow": discharge_windows_tomorrow,
        "deye_tou_schedule_tomorrow": tou_tomorrow,
        # Day price frames (pricing only) so cache hits and the sync path rebuild TOU from the same profile
        "pricing_frames_today": [_pricing_only(f) for f in frames_today],
        "pricing_frames_tomorrow": [_pricing_only(f) for f in frames_tomorrow] if has_tomorrow_pricing else [],
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
    parser.add_argument("--tomorrow", action="store_true", help="Target tomorrow's schedule instead of today")
    parser.add_argument("--print-tou", action="store_true", help="Print human-readable ASCII TOU schedule table without writing to inverter")
    parser.add_argument("--force", action="store_true", help="Force TOU write even if schedule is identical to current inverter settings")
    parser.add_argument("--max-charge-current", type=float, default=None, help="Inverter max charge current override (Amperes)")
    parser.add_argument("--max-discharge-current", type=float, default=None, help="Inverter max discharge current override (Amperes)")
    parser.add_argument("--set-charge-current", type=int, default=None, help="Modulate Max Charge Current (Reg 108 in Amperes)")
    parser.add_argument("--restore-charge-current", action="store_true", help="Restore Max Charge Current (Reg 108) to baseline")
    parser.add_argument("--battery-capacity", type=float, default=None, help="Battery total capacity in kWh (dynamically resolved from Deye/env if omitted)")
    parser.add_argument("--nominal-voltage", type=float, default=None, help="Nominal battery pack voltage in Volts (dynamically resolved from Deye/env if omitted)")
    parser.add_argument("--pre-peak-margin", type=int, default=energy_defaults.DEFAULT_PRE_PEAK_MARGIN, help="Pre-peak daytime autokonsumpcja margin in percent")
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
            pre_peak_margin=args.pre_peak_margin,
            loss_buffer_pct=args.loss_buffer_pct,
            sell_target_hours=sell_hours,
            ev_target_hours=ev_hours,
        )
        if not isinstance(data, dict) or "error" in data:
            print(f"Błąd pobierania danych: {data.get('error') if isinstance(data, dict) else data}")
            return 1

        target_sched_key = "deye_tou_schedule_tomorrow" if args.tomorrow else "deye_tou_schedule_today"
        schedule_label = "JUTRO (TOMORROW)" if args.tomorrow else "DZIŚ (TODAY)"
        slots = data.get(target_sched_key, [])
        if not slots and not args.tomorrow:
            slots = data.get("deye_tou_schedule", [])

        print(f"\n=== HARMONOGRAM TOU DEYE — {schedule_label} ===")
        if not slots:
            print(f"Brak harmonogramu dla {schedule_label} (publikacja cen na jutro ok. 12:00-14:00).")
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
            for_tomorrow=args.tomorrow,
            force=args.force,
            max_charge_current=args.max_charge_current,
            max_discharge_current=args.max_discharge_current,
            battery_capacity_kwh=args.battery_capacity,
            nominal_voltage=args.nominal_voltage,
            pre_peak_margin=args.pre_peak_margin,
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
        pre_peak_margin=args.pre_peak_margin,
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
