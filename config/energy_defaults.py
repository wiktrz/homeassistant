#!/usr/bin/env python3
"""
Single Source of Truth for Energy, Battery, and Inverter Defaults.
All Python modules must import their fallback values from this module.
Never define inline fallback literals in client modules or function signatures.

Hardware fallback values (e.g. battery capacity, voltage, charge current)
reside strictly in the environment template (.env.example) and real .env.
This module dynamically parses .env / .env.example at load time.
"""

import os
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_ALIASES = [
    ("BATTERY_CHARGE_CURRENT_A", "BATTERY_MAX_CHARGE_CURRENT"),
]


def _sync_aliases(d: Dict[str, str], changed_key: Optional[str] = None) -> None:
    for a1, a2 in _ALIASES:
        if changed_key == a1 and a1 in d:
            d[a2] = d[a1]
        elif changed_key == a2 and a2 in d:
            d[a1] = d[a2]
        elif a1 in d and a2 not in d:
            d[a2] = d[a1]
        elif a2 in d and a1 not in d:
            d[a1] = d[a2]


def _parse_env_file(path: str, target_dict: Dict[str, str], overwrite: bool = False) -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and (overwrite or k not in target_dict):
                    target_dict[k] = v
                    _sync_aliases(target_dict, changed_key=k)
    except Exception:
        pass


def _load_all_env_defaults() -> Dict[str, str]:
    env_vars: Dict[str, str] = {}

    # 1. Parse .env.example as the baseline template (canonical defaults)
    example_paths = [
        os.path.join(_REPO_ROOT, ".env.example"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.example"),
        "/config/.env.example",
        os.path.join(os.getcwd(), ".env.example"),
    ]
    for p in example_paths:
        if os.path.isfile(p):
            _parse_env_file(p, env_vars, overwrite=False)
            break

    # 2. Overlay real .env files if present (user configuration overrides baseline)
    env_paths = [
        "/config/.env",
        os.path.join(_REPO_ROOT, ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for p in env_paths:
        if os.path.isfile(p):
            _parse_env_file(p, env_vars, overwrite=True)
            break

    # 3. System environment variables take top priority
    for k, v in os.environ.items():
        if v:
            env_vars[k] = v
            _sync_aliases(env_vars, changed_key=k)

    return env_vars


_CONFIG = _load_all_env_defaults()


def get_default_str(key: str, alias: Optional[str] = None) -> str:
    val = _CONFIG.get(key)
    if (val is None or val == "") and alias:
        val = _CONFIG.get(alias)
    return str(val).strip() if val is not None else ""


def get_default_float(key: str, alias: Optional[str] = None) -> float:
    s = get_default_str(key, alias=alias)
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def get_default_int(key: str, alias: Optional[str] = None) -> int:
    s = get_default_str(key, alias=alias)
    try:
        return int(float(s)) if s else 0
    except ValueError:
        return 0


def get_default(key: str, alias: Optional[str] = None) -> Any:
    return _CONFIG.get(key) if key in _CONFIG else (_CONFIG.get(alias) if alias else None)


# --- 1. Battery Hardware Fallbacks (Rule 9 Zero-Hardcoding) ---
# Values are parsed directly from .env / .env.example
FALLBACK_BATTERY_CAPACITY_KWH = get_default_float("BATTERY_CAPACITY_KWH")
FALLBACK_BATTERY_NOMINAL_VOLTAGE = get_default_float("BATTERY_NOMINAL_VOLTAGE")
FALLBACK_BATTERY_MAX_CHARGE_CURRENT = get_default_float(
    "BATTERY_MAX_CHARGE_CURRENT", alias="BATTERY_CHARGE_CURRENT_A"
)
FALLBACK_BATTERY_CAPACITY_AH = get_default_float("BATTERY_CAPACITY_AH")

# --- 2. Battery Cell Physics ---
LIFEPO4_CELL_COUNT = 16
LIFEPO4_CELL_NOMINAL_V = 3.2
LIFEPO4_16S_VOLTAGE_LOW = 44.0
LIFEPO4_16S_VOLTAGE_HIGH = 58.4
BATTERY_PRESENCE_VOLTAGE_THRESHOLD = 40.0

# --- 3. Time-of-Use (TOU) & SOC Dispatch Defaults ---
DEFAULT_MAX_SOC = get_default_int("DEYE_DEFAULT_MAX_SOC")
DEFAULT_MIN_SOC = get_default_int("DEYE_DEFAULT_MIN_SOC")
DEFAULT_SHUTDOWN_SOC = get_default_int("DEYE_DEFAULT_SHUTDOWN_SOC")
DEFAULT_CURRENT_SOC = get_default_int("DEYE_DEFAULT_CURRENT_SOC")
DEFAULT_MORNING_RESERVE_MARGIN = get_default_int("DEYE_DEFAULT_MORNING_RESERVE_MARGIN")
DEFAULT_PRE_PEAK_MARGIN = get_default_int("DEYE_DEFAULT_PRE_PEAK_MARGIN")
DEFAULT_LOSS_BUFFER_PCT = get_default_float("DEYE_DEFAULT_LOSS_BUFFER_PCT")

# --- 4. Inverter Power Limits (Watts) ---
INVERTER_MAX_POWER_W = get_default_int("DEYE_INVERTER_MAX_POWER_W")
INVERTER_MIN_POWER_W = get_default_int("DEYE_INVERTER_MIN_POWER_W")
DEFAULT_TRANSFER_POWER_W = get_default_int("DEYE_DEFAULT_TRANSFER_POWER_W")
CALIBRATION_MIN_POWER_W = get_default_int("DEYE_CALIBRATION_MIN_POWER_W")

# --- 5. Network & Cache Defaults ---
DEFAULT_INVERTER_IP = get_default_str("DEYE_INVERTER_IP")
DEFAULT_INVERTER_PORT = get_default_int("DEYE_INVERTER_PORT")
DEFAULT_LOGGER_SN = get_default_str("DEYE_LOGGER_SN")
DEFAULT_INVERTER_SN = get_default_str("DEYE_INVERTER_SN")
DEFAULT_SOCKET_TIMEOUT = get_default_float("DEYE_SOCKET_TIMEOUT")
DEYE_CACHE_TTL = get_default_float("DEYE_CACHE_TTL_SECONDS")
PSTRYK_CACHE_TTL = get_default_float("PSTRYK_CACHE_TTL_SECONDS")

# --- 6. Pstryk API & Energy Market Defaults ---
DEFAULT_HOURS_AHEAD = 48
DEFAULT_INSTALLATION = "dol"
DEFAULT_HTTP_TIMEOUT = get_default_int("PSTRYK_HTTP_TIMEOUT")
VAT_MULTIPLIER = get_default_float("ENERGY_VAT_RATE")
PRICE_EXPANSION_TOLERANCE = 0.05
PRICE_EXPANSION_EPSILON = 0.01

# --- 7. Window Durations ---
EV_WINDOW_WEEKDAY_HOURS = 2
EV_WINDOW_WEEKEND_HOURS = 4
PB_BASE_HOURS = 2
PB_MAX_WEEKEND_HOURS = 5
SELL_TARGET_HOURS = 3

# --- 8. Slot Classification Thresholds ---
TOU_HOLD_SOC_THRESHOLD = 80
TOU_DISCHARGE_SOC_THRESHOLD = 30
TOU_POWER_DIFF_TOLERANCE_W = 50
