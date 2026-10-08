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
import zoneinfo
from typing import Dict, Any, List, Optional, Tuple

WARSAW_TZ = zoneinfo.ZoneInfo("Europe/Warsaw")

DEFAULT_API_KEY = "sk-YOUR_TOKEN_HERE"
BASE_URL = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/"
CACHE_TTL_SECONDS = 900  # 15 minutes
DEFAULT_CACHE_DIR = "/tmp"


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

    # 1. Explicit key argument
    if explicit_key and explicit_key not in (DEFAULT_API_KEY, "None", ""):
        if not explicit_key.startswith("!secret") and not explicit_key.startswith("sk-YOUR_"):
            return explicit_key, "arg"

    # 2. Installation-specific env var from .env
    inst_env_var = f"PSTRYK_API_KEY_{clean_inst.upper()}"
    env_inst_key = os.getenv(inst_env_var)
    if env_inst_key and env_inst_key not in (DEFAULT_API_KEY, "None", "") and not env_inst_key.startswith("sk-YOUR_"):
        return env_inst_key, "env"

    # 3. Generic env var from .env
    generic_env = os.getenv("PSTRYK_API_KEY")
    if generic_env and generic_env not in (DEFAULT_API_KEY, "None", "") and not generic_env.startswith("sk-YOUR_"):
        return generic_env, "env"

    # 4. Fallback: secrets.yaml
    sec_key = load_secrets_yaml_key(clean_inst)
    if sec_key:
        return sec_key, "secrets_yaml"

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


class BatteryModel:
    """Mathematical and physical model of battery pack with configurable SOC bounds."""
    def __init__(
        self,
        capacity_kwh: float = 16.13,
        max_soc: float = 90.0,
        min_soc: float = 20.0,
        shutdown_soc: float = 5.0,
        charge_current_a: float = 100.0,
        discharge_current_a: float = 100.0,
        voltage_v: float = 51.2,
    ):
        self.capacity_kwh = float(capacity_kwh)
        self.max_soc = max(50.0, min(100.0, float(max_soc)))
        self.min_soc = max(10.0, min(50.0, float(min_soc)))
        self.shutdown_soc = max(0.0, min(15.0, float(shutdown_soc)))
        self.charge_current_a = float(charge_current_a)
        self.discharge_current_a = float(discharge_current_a)
        self.voltage_v = float(voltage_v)

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

    ref_dt = now_dt or datetime.now(timezone.utc)
    if target_hours == "auto" or target_hours is None:
        first_start = valid_frames[0].get("start")
        if first_start:
            try:
                frame_dt = datetime.fromisoformat(first_start.replace("Z", "+00:00"))
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


def find_powerbank_best_window(
    data: Dict[str, Any],
    target_hours: int = 2,
    allow_disjoint: bool = True
) -> Dict[str, Any]:
    """
    Finds the optimal window for home battery (Power Bank) charging.
    Can be contiguous or disjoint (e.g., 1h at night + 1h during midday solar dip).
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
    n_hours = max(1, min(len(search_frames), target_hours))

    def get_gross(f):
        p = f["metrics"]["pricing"]
        if p.get("price_gross") is not None:
            return float(p["price_gross"])
        if p.get("full_price") is not None:
            return float(p["full_price"])
        if p.get("price_net") is not None:
            return round(float(p["price_net"]) * 1.23, 4)
        return 999.0

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


def generate_deye_tou_schedule(
    current_soc: int = 85,
    max_soc: int = 90,
    min_soc: int = 20,
    best_pb_window: Optional[Dict[str, Any]] = None,
    best_sell_window: Optional[Dict[str, Any]] = None,
    calibration_active: bool = False,
    default_power_w: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Generates a 6-slot Time-of-Use (TOU) schedule for Deye Hybrid Inverter.
    Slots adapt dynamically to place the 2-hour charge slot during the cheapest prices
    (whether midday solar dip e.g. 13:00-15:00 or night e.g. 02:00-04:00), holding charge
    until the major peak, and discharging to 100% cover house load during the peak.
    """
    def to_minutes(hhmm_str: Optional[str], default_m: int) -> int:
        if not hhmm_str or hhmm_str == "--:--":
            return default_m
        try:
            parts = hhmm_str.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return default_m

    def to_time_str(m: int) -> str:
        hh = (m // 60) % 24
        mm = m % 60
        return f"{hh:02d}:{mm:02d}"

    safe_max_soc = 100 if calibration_active else max(50, min(100, int(max_soc)))
    safe_min_soc = max(10, min(50, int(min_soc)))

    pb_start_m = 120  # Default 02:00 (safe night charge fallback if no pricing data)
    pb_end_m = 240    # Default 04:00
    has_disjoint_midday = False
    disjoint_midday_m = 780
    disjoint_night_m = 120

    if isinstance(best_pb_window, dict) and "error" not in best_pb_window:
        if not best_pb_window.get("is_consecutive", True):
            slots_list = best_pb_window.get("slots", [])
            midday_slots = []
            night_slots = []
            for sl in slots_list:
                st = sl.split(" - ")[0].strip()
                sm = to_minutes(st, 0)
                if sm >= 480:
                    midday_slots.append(sm)
                else:
                    night_slots.append(sm)
            if midday_slots and night_slots:
                has_disjoint_midday = True
                disjoint_midday_m = midday_slots[0]
                disjoint_night_m = night_slots[0]

        pb_start_str = format_hhmm(best_pb_window.get("start"))
        pb_end_str = format_hhmm(best_pb_window.get("end"))
        pb_start_m = to_minutes(pb_start_str, 780)
        pb_end_m = to_minutes(pb_end_str, 900)
        if pb_end_m <= pb_start_m:
            pb_end_m = pb_start_m + 120

    sell_start_m = 1020  # Default 17:00
    sell_end_m = 1260    # Default 21:00
    if isinstance(best_sell_window, dict) and "error" not in best_sell_window:
        sell_start_str = format_hhmm(best_sell_window.get("start"))
        sell_end_str = format_hhmm(best_sell_window.get("end"))
        sell_start_m = to_minutes(sell_start_str, 1020)
        sell_end_m = to_minutes(sell_end_str, 1260)
        if sell_end_m <= sell_start_m:
            sell_end_m = sell_start_m + 180

    is_midday = (pb_start_m >= 480) and not has_disjoint_midday

    if has_disjoint_midday:
        t1 = disjoint_night_m
        t2 = t1 + 60
        t3 = max(t2 + 60, disjoint_midday_m)
        t4 = t3 + 60
        t5 = max(t4 + 60, sell_start_m)
        t6 = max(t5 + 60, sell_end_m)
        times = [t1, t2, t3, t4, t5, t6]
        for idx in range(1, len(times)):
            if times[idx] <= times[idx - 1]:
                times[idx] = min(1410, times[idx - 1] + 30)

        slots = [
            {
                "slot": 1,
                "time": to_time_str(times[0]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": True,
                "label": "Ładowanie (Kalibracja 100%)" if calibration_active else f"Ładowanie Nocne ({to_time_str(times[0])}-{to_time_str(times[1])})",
            },
            {
                "slot": 2,
                "time": to_time_str(times[1]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": False,
                "label": "Czuwanie Poranne (Hold)",
            },
            {
                "slot": 3,
                "time": to_time_str(times[2]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": True,
                "label": "Ładowanie (Kalibracja 100%)" if calibration_active else f"Tania Godzina Dzienna ({to_time_str(times[2])}-{to_time_str(times[3])})",
            },
            {
                "slot": 4,
                "time": to_time_str(times[3]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": False,
                "label": "Rezerwa Przed Szczytem (Hold)",
            },
            {
                "slot": 5,
                "time": to_time_str(times[4]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": f"Szczyt Wieczorny 100% ({to_time_str(times[4])}-{to_time_str(times[5])})",
            },
            {
                "slot": 6,
                "time": to_time_str(times[5]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": "Autokonsumpcja Nocna",
            },
        ]
    elif is_midday:
        t1 = 0
        t2 = min(pb_start_m - 60, 360)
        t3 = pb_start_m
        t4 = max(t3 + 60, pb_end_m)
        t5 = max(t4 + 60, sell_start_m)
        t6 = max(t5 + 60, sell_end_m)
        times = [t1, t2, t3, t4, t5, t6]
        # Guarantee strict monotonicity
        for idx in range(1, len(times)):
            if times[idx] <= times[idx - 1]:
                times[idx] = min(1410, times[idx - 1] + 30)

        slots = [
            {
                "slot": 1,
                "time": to_time_str(times[0]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": "Czuwanie Nocne",
            },
            {
                "slot": 2,
                "time": to_time_str(times[1]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": "Szczyt Poranny (Autokonsumpcja)",
            },
            {
                "slot": 3,
                "time": to_time_str(times[2]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": True,
                "label": "Ładowanie (Kalibracja 100%)" if calibration_active else f"Najtańsze Ładowanie ({to_time_str(times[2])}-{to_time_str(times[3])})",
            },
            {
                "slot": 4,
                "time": to_time_str(times[3]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": False,
                "label": "Czuwanie Przed Szczytem (Hold)",
            },
            {
                "slot": 5,
                "time": to_time_str(times[4]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": f"Szczyt Wieczorny 100% ({to_time_str(times[4])}-{to_time_str(times[5])})",
            },
            {
                "slot": 6,
                "time": to_time_str(times[5]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": "Autokonsumpcja Nocna",
            },
        ]
    else:
        t1 = pb_start_m
        t2 = max(t1 + 60, pb_end_m)
        t3 = max(t2 + 60, 360)
        t4 = max(t3 + 60, 720)
        t5 = max(t4 + 60, sell_start_m)
        t6 = max(t5 + 60, sell_end_m)
        times = [t1, t2, t3, t4, t5, t6]
        # Guarantee strict monotonicity
        for idx in range(1, len(times)):
            if times[idx] <= times[idx - 1]:
                times[idx] = min(1410, times[idx - 1] + 30)

        slots = [
            {
                "slot": 1,
                "time": to_time_str(times[0]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": True,
                "label": "Ładowanie (Kalibracja 100%)" if calibration_active else f"Najtańsze Ładowanie ({to_time_str(times[0])}-{to_time_str(times[1])})",
            },
            {
                "slot": 2,
                "time": to_time_str(times[1]),
                "power_w": default_power_w,
                "target_soc": safe_max_soc,
                "grid_charge": False,
                "label": "Czuwanie Poranne (Hold)",
            },
            {
                "slot": 3,
                "time": to_time_str(times[2]),
                "power_w": default_power_w,
                "target_soc": max(safe_min_soc + 20, 50),
                "grid_charge": False,
                "label": "Szczyt Poranny (Autokonsumpcja)",
            },
            {
                "slot": 4,
                "time": to_time_str(times[3]),
                "power_w": default_power_w,
                "target_soc": max(safe_min_soc + 20, 50),
                "grid_charge": False,
                "label": "Autokonsumpcja PV",
            },
            {
                "slot": 5,
                "time": to_time_str(times[4]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": f"Szczyt Wieczorny 100% ({to_time_str(times[4])}-{to_time_str(times[5])})",
            },
            {
                "slot": 6,
                "time": to_time_str(times[5]),
                "power_w": default_power_w,
                "target_soc": safe_min_soc,
                "grid_charge": False,
                "label": "Czuwanie Nocne",
            },
        ]

    return slots


def sync_deye_inverter_tou_schedule(
    host: Optional[str] = None,
    port: Optional[int] = None,
    current_soc: Optional[int] = None,
    max_soc: Optional[int] = None,
    min_soc: Optional[int] = None,
    calibration_active: bool = False,
    api_key: Optional[str] = None,
    installation: str = "dol",
    cache_dir: str = DEFAULT_CACHE_DIR,
    for_tomorrow: bool = False,
) -> Dict[str, Any]:
    """
    Recalculates and uploads the 6-slot TOU schedule to the Deye hybrid inverter via Solarman V5 client.
    Reads live inverter telemetry and config when available, and derives optimal windows from Pstryk pricing data.
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

    eff_max_soc = max_soc if max_soc is not None else inv_cfg.get("max_charge_soc", 90)
    eff_min_soc = min_soc if min_soc is not None else inv_cfg.get("min_discharge_soc", 20)
    eff_current_soc = current_soc if current_soc is not None else inv_tlm.get("battery_soc", 85)

    # Resolve pricing windows (from cache or API)
    cache_file = get_cache_file_path(installation, cache_dir)
    cached_data = read_cache(cache_file)
    pb_window = None
    sell_window = None

    if cached_data and isinstance(cached_data, dict):
        if for_tomorrow and cached_data.get("has_tomorrow_pricing"):
            pb_window = cached_data.get("powerbank_best_window_tomorrow")
            sell_window = cached_data.get("best_sell_window_tomorrow")
        else:
            pb_window = cached_data.get("powerbank_best_window")
            sell_window = cached_data.get("best_sell_window")

    if not pb_window or not sell_window:
        resolved_key, _ = resolve_api_key(installation=installation, explicit_key=api_key)
        if resolved_key:
            try:
                fresh_data = fetch_consolidated_data(api_key=resolved_key, installation=installation, cache_dir=cache_dir)
                if isinstance(fresh_data, dict):
                    if for_tomorrow and fresh_data.get("has_tomorrow_pricing"):
                        pb_window = fresh_data.get("powerbank_best_window_tomorrow")
                        sell_window = fresh_data.get("best_sell_window_tomorrow")
                    else:
                        pb_window = fresh_data.get("powerbank_best_window")
                        sell_window = fresh_data.get("best_sell_window")
            except Exception:
                pass

    slots = generate_deye_tou_schedule(
        current_soc=eff_current_soc,
        max_soc=eff_max_soc,
        min_soc=eff_min_soc,
        best_pb_window=pb_window,
        best_sell_window=sell_window,
        calibration_active=calibration_active,
    )

    sync_result = {"success": False, "message": "solarman_v5_client module not available"}
    if solarman_v5_client is not None:
        try:
            sync_result = solarman_v5_client.sync_tou_schedule(slots=slots, host=host, port=port)
        except Exception as e:
            sync_result = {"success": False, "error": str(e)}

    # Direct fallback to Deye Cloud OpenAPI if local Modbus was not successful
    if not sync_result.get("success"):
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

    return {
        "success": sync_result.get("success", False),
        "timestamp": datetime.now().isoformat(),
        "host": host,
        "port": port,
        "calibration_active": calibration_active,
        "soc_limits": {
            "current_soc": eff_current_soc,
            "max_soc": eff_max_soc,
            "min_soc": eff_min_soc,
        },
        "slots": slots,
        "inverter_response": sync_result,
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


def format_hhmm(iso_str: Optional[str], tz: Optional[zoneinfo.ZoneInfo] = None) -> str:
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


def fetch_consolidated_data(
    api_key: Optional[str] = None,
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
    if not api_key or api_key == DEFAULT_API_KEY:
        resolved_k, _ = resolve_api_key(installation=installation)
        if resolved_k:
            api_key = resolved_k
        else:
            api_key = DEFAULT_API_KEY

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
    frames_tomorrow = [f for f in all_frames if get_frame_warsaw_date(f) == tomorrow_date]
    has_tomorrow_pricing = len(frames_tomorrow) >= 12

    # Dispatch windows for TODAY (stable across all 24h of the current day)
    target_today_data = {"frames": frames_today} if frames_today else (forward_data if (isinstance(forward_data, dict) and forward_data.get("frames")) else today_data)
    ev_window_today = find_ev_best_window(target_today_data, target_hours="auto", now_dt=now_warsaw)
    pb_window_today = find_powerbank_best_window(target_today_data, target_hours=2, allow_disjoint=True)
    sell_window_today = find_best_sell_window(target_today_data, target_hours=3)
    tou_today = generate_deye_tou_schedule(
        current_soc=85,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_window_today,
        best_sell_window=sell_window_today,
        calibration_active=False,
    )

    # Dispatch windows for TOMORROW (published ~12:00 Warsaw time)
    if has_tomorrow_pricing:
        target_tomorrow_data = {"frames": frames_tomorrow}
        tomorrow_ref_dt = now_warsaw + timedelta(days=1)
        ev_window_tomorrow = find_ev_best_window(target_tomorrow_data, target_hours="auto", now_dt=tomorrow_ref_dt)
        pb_window_tomorrow = find_powerbank_best_window(target_tomorrow_data, target_hours=2, allow_disjoint=True)
        sell_window_tomorrow = find_best_sell_window(target_tomorrow_data, target_hours=3)
        tou_tomorrow = generate_deye_tou_schedule(
            current_soc=85,
            max_soc=90,
            min_soc=20,
            best_pb_window=pb_window_tomorrow,
            best_sell_window=sell_window_tomorrow,
            calibration_active=False,
        )
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
        tou_tomorrow = []

    # Battery model defaults for Deye 12kW + SunDeposit 16.13 kWh
    battery_model = BatteryModel(
        capacity_kwh=16.13,
        max_soc=90.0,
        min_soc=20.0,
        shutdown_soc=5.0,
        charge_current_a=100.0,
        discharge_current_a=100.0,
        voltage_v=51.2,
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
        "deye_tou_schedule": tou_today,
        "ev_best_window_today": ev_window_today,
        "powerbank_best_window_today": pb_window_today,
        "best_sell_window_today": sell_window_today,
        "deye_tou_schedule_today": tou_today,
        "ev_best_window_tomorrow": ev_window_tomorrow,
        "powerbank_best_window_tomorrow": pb_window_tomorrow,
        "best_sell_window_tomorrow": sell_window_tomorrow,
        "deye_tou_schedule_tomorrow": tou_tomorrow,
        "has_tomorrow_pricing": has_tomorrow_pricing,
        "battery_model": battery_model.to_dict(),
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
    parser.add_argument("--sync-deye-tou", action="store_true", help="Recalculate and synchronize 6-slot TOU schedule to Deye inverter")
    parser.add_argument("--inverter-host", default=None, help="Deye Inverter logger IP (defaults to configured IP)")
    parser.add_argument("--inverter-port", type=int, default=None, help="Deye Inverter logger Port (defaults to configured port)")
    parser.add_argument("--current-soc", type=int, default=None, help="Battery SOC override")
    parser.add_argument("--max-soc", type=int, default=None, help="Max Charge SOC override")
    parser.add_argument("--min-soc", type=int, default=None, help="Min Discharge SOC override")
    parser.add_argument("--calibration", action="store_true", help="Flag to charge to 100 percent for BMS calibration")
    parser.add_argument("--tomorrow", action="store_true", help="Target tomorrow's schedule instead of today")

    args = parser.parse_args(argv)
    resolved_key, key_source = resolve_api_key(args.installation, args.key)
    api_key = resolved_key or DEFAULT_API_KEY

    if args.sync_deye_tou:
        sync_res = sync_deye_inverter_tou_schedule(
            host=args.inverter_host,
            port=args.inverter_port,
            current_soc=args.current_soc,
            max_soc=args.max_soc,
            min_soc=args.min_soc,
            calibration_active=args.calibration,
            api_key=resolved_key,
            installation=args.installation,
            for_tomorrow=args.tomorrow,
        )
        print(json.dumps(sync_res, indent=2))
        return 0 if sync_res.get("success") else 1

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
    )

    if isinstance(result, dict):
        result["debug"] = debug_info
        result["installation"] = args.installation

    print(json.dumps(result, indent=2))
    return 0 if (isinstance(result, dict) and "error" not in result) else 1


if __name__ == "__main__":
    sys.exit(main())
