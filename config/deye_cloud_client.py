#!/usr/bin/env python3
"""
Deye Cloud OpenAPI Client for Deye Hybrid Inverters & SunDeposit Storage.
Target Hardware: Deye SUN-12K-SG05LP3-EU-SM2 (Sub-device SN: 2603160727) + DYDA_WiBLE_1.6.2 Logger.
API Documentation: https://developer.deyecloud.com/api
Base URL: https://eu1-developer.deyecloud.com (Europe / logger-eu)

Functions:
- Automatic environment loading from .env or config/.env
- Token acquisition via App ID/Secret or direct apiKey
- Telemetry ingestion (Battery SOC, Voltage, Power, PV Yields)
- Time-of-Use (TOU) 6-slot schedule dispatch
"""

import os
import sys
import json
import time
import hashlib
import urllib.request
import urllib.error
import urllib.parse
import argparse
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

DEFAULT_API_URL = "https://eu1-developer.deyecloud.com"
DEFAULT_LOGGER_SN = "D26213439330"
DEFAULT_INVERTER_SN = "2603160727"
DEFAULT_TOKEN_CACHE_FILE = "/tmp/deye_cloud_token.json"


def hash_deye_password(pwd: Optional[str]) -> Optional[str]:
    """Ensures password is lower-case SHA256 hashed as required by Deye OpenAPI specification."""
    if not pwd:
        return pwd
    # If already a 64-character hex string, keep lowercase
    if len(pwd) == 64 and all(c in "0123456789abcdefABCDEF" for c in pwd):
        return pwd.lower()
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest().lower()


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
                break
            except Exception:
                pass


# Execute dotenv loading on import
load_dotenv()


def get_deye_cloud_config() -> Dict[str, Any]:
    """Retrieves Deye Cloud configuration from environment variables."""
    load_dotenv()
    return {
        "api_url": os.getenv("DEYE_CLOUD_API_URL", DEFAULT_API_URL).rstrip("/"),
        "api_key": os.getenv("DEYE_CLOUD_API_KEY"),
        "app_id": os.getenv("DEYE_CLOUD_APP_ID"),
        "app_secret": os.getenv("DEYE_CLOUD_APP_SECRET"),
        "email": os.getenv("DEYE_CLOUD_EMAIL"),
        "password": os.getenv("DEYE_CLOUD_PASSWORD"),
        "logger_sn": os.getenv("DEYE_LOGGER_SN", DEFAULT_LOGGER_SN),
        "inverter_sn": os.getenv("DEYE_INVERTER_SN", DEFAULT_INVERTER_SN),
        "inverter_ip": os.getenv("DEYE_INVERTER_IP", "10.20.2.6"),
        "inverter_port": int(os.getenv("DEYE_INVERTER_PORT", "8899")),
    }


def get_access_token(cfg: Optional[Dict[str, Any]] = None, cache_file: str = DEFAULT_TOKEN_CACHE_FILE) -> Tuple[Optional[str], str]:
    """
    Obtains access token for Deye Cloud API:
    1. Direct API Key (if provided)
    2. Cached token from disk
    3. Token request via POST /v1.0/account/token using App ID/Secret or Email/Password
    """
    if cfg is None:
        cfg = get_deye_cloud_config()

    # 1. Direct API Key takes precedence
    if cfg.get("api_key") and cfg["api_key"] not in ("your_deye_api_key_here", "twoj_klucz_api_deye_cloud_tutaj", ""):
        return cfg["api_key"], "api_key"

    # 2. Check token cache
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached.get("token") and cached.get("expires_at", 0) > time.time() + 60:
                    return cached["token"], "cached"
        except Exception:
            pass

    # 3. Request token via POST /v1.0/account/token?appId={appId}
    app_id = cfg.get("app_id")
    app_secret = cfg.get("app_secret")
    email = cfg.get("email")
    password = cfg.get("password")

    if not app_id or not app_secret:
        return None, "Missing credentials (set DEYE_CLOUD_APP_ID and DEYE_CLOUD_APP_SECRET in .env, or DEYE_CLOUD_API_KEY)"

    if not email or not password:
        return None, "Missing account credentials (Deye OpenAPI requires DEYE_CLOUD_EMAIL and DEYE_CLOUD_PASSWORD alongside DEYE_CLOUD_APP_ID and DEYE_CLOUD_APP_SECRET to obtain accessToken)"

    token_url = f"{cfg['api_url']}/v1.0/account/token?appId={urllib.parse.quote(str(app_id))}"

    payload: Dict[str, Any] = {
        "appSecret": app_secret,
        "email": email,
        "password": hash_deye_password(password),
    }

    try:
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            token_url,
            data=req_data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if isinstance(body, dict):
                # Check for explicit Deye error response
                if body.get("success") is False or body.get("code") not in (0, "0", 1000000, "1000000", None):
                    code = str(body.get("code", ""))
                    msg = body.get("msg", "API error")
                    if code in ("2101006", "2101008", "2101018", "2101021"):
                        return None, f"Deye Cloud error [{code}]: {msg} (Verify DEYE_CLOUD_APP_ID, APP_SECRET, and account login)"
                    return None, f"Deye Cloud error [{code}]: {msg}"

                token = (
                    body.get("accessToken")
                    or body.get("token")
                    or body.get("data", {}).get("accessToken")
                    or body.get("data", {}).get("token")
                )
                expires_in = (
                    body.get("expiresIn")
                    or body.get("data", {}).get("expiresIn")
                    or 86400 * 60
                )

                if token:
                    try:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump({"token": token, "expires_at": time.time() + expires_in}, f)
                    except Exception:
                        pass
                    return token, "new_token"
            return None, f"Token not found in response: {body}"
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        return None, f"HTTP {e.code}: {err_msg}"
    except Exception as e:
        return None, f"Token request failed: {e}"


def call_deye_api(
    endpoint: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Any, str]:
    """Executes authenticated request to Deye Cloud OpenAPI."""
    if cfg is None:
        cfg = get_deye_cloud_config()

    token, token_status = get_access_token(cfg)
    if not token:
        return False, None, f"Authentication error: {token_status}"

    raw_token = token[7:] if token.startswith("Bearer ") else token
    url = f"{cfg['api_url']}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {raw_token}",
        "x-deye-token": raw_token,
    }

    req_data = json.dumps(data).encode("utf-8") if data else None
    try:
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
            res_json = json.loads(raw)
            return True, res_json, "OK"
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        return False, None, f"HTTP Error {e.code}: {err_body}"
    except Exception as e:
        return False, None, f"Request failed: {e}"


def read_inverter_telemetry(
    inverter_sn: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Reads live inverter telemetry, configuration, and battery metrics via Deye Cloud OpenAPI.
    Returns standard dictionary matching Solarman V5 format for seamless HA integration.
    """
    if cfg is None:
        cfg = get_deye_cloud_config()
    target_sn = inverter_sn or cfg.get("inverter_sn", DEFAULT_INVERTER_SN)
    timestamp = datetime.now().isoformat()

    # Default fallback data structure
    config_data = {
        "max_charge_current": 100,
        "max_discharge_current": 100,
        "min_discharge_soc": 20,
        "shutdown_soc": 5,
        "max_charge_soc": 90,
    }
    telemetry_data = {
        "battery_voltage": 51.2,
        "battery_soc": 85,
        "battery_power": 0,
        "battery_current": 0.0,
        "pv_power": 0,
        "daily_yield": 0.0,
        "total_yield": 0.0,
    }
    active_tou = {
        "enabled": True,
        "slots": [],
    }

    # 1. Attempt telemetry query
    query_endpoints = [
        f"/v1.0/device/data?deviceSn={target_sn}",
        f"/v1.0/device/latest?deviceSn={target_sn}",
        f"/v1.0/config/battery?deviceSn={target_sn}",
    ]

    cloud_ok = False
    last_err = "No endpoints answered"

    for ep in query_endpoints:
        ok, res, msg = call_deye_api(ep, method="GET", cfg=cfg)
        if ok and isinstance(res, dict):
            cloud_ok = True
            payload = res.get("data", res)

            # Extract battery SOC
            if "soc" in payload or "batterySoc" in payload:
                telemetry_data["battery_soc"] = int(payload.get("soc", payload.get("batterySoc", 85)))
            if "batteryVoltage" in payload or "vBat" in payload:
                telemetry_data["battery_voltage"] = float(payload.get("batteryVoltage", payload.get("vBat", 51.2)))
            if "batteryPower" in payload or "pBat" in payload:
                telemetry_data["battery_power"] = int(payload.get("batteryPower", payload.get("pBat", 0)))
            if "batteryCurrent" in payload or "iBat" in payload:
                telemetry_data["battery_current"] = float(payload.get("batteryCurrent", payload.get("iBat", 0.0)))
            if "activePower" in payload or "currentPower" in payload:
                telemetry_data["pv_power"] = int(payload.get("activePower", payload.get("currentPower", 0)))
            if "dailyYield" in payload or "yieldToday" in payload:
                telemetry_data["daily_yield"] = float(payload.get("dailyYield", payload.get("yieldToday", 0.0)))
            if "totalYield" in payload:
                telemetry_data["total_yield"] = float(payload.get("totalYield", 0.0))

            # Extract config parameters if available
            if "maxChargeSoc" in payload:
                config_data["max_charge_soc"] = int(payload["maxChargeSoc"])
            if "minDischargeSoc" in payload:
                config_data["min_discharge_soc"] = int(payload["minDischargeSoc"])
            if "shutdownSoc" in payload:
                config_data["shutdown_soc"] = int(payload["shutdownSoc"])
            if "maxChargeCurrent" in payload:
                config_data["max_charge_current"] = int(payload["maxChargeCurrent"])
            if "maxDischargeCurrent" in payload:
                config_data["max_discharge_current"] = int(payload["maxDischargeCurrent"])
            break
        else:
            last_err = msg

    return {
        "success": cloud_ok,
        "source": "deye_cloud_openapi",
        "timestamp": timestamp,
        "device_sn": target_sn,
        "logger_sn": cfg.get("logger_sn", DEFAULT_LOGGER_SN),
        "api_url": cfg.get("api_url"),
        "config": config_data,
        "telemetry": telemetry_data,
        "active_tou": active_tou,
        "error": None if cloud_ok else last_err,
    }


def sync_tou_schedule(
    slots: List[Dict[str, Any]],
    inverter_sn: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Transmits Time-of-Use schedule to Deye inverter via Deye Cloud OpenAPI.
    Endpoint: /v1.0/order/sys/energyPattern/update or /v1.0/order/battery/parameter/update
    """
    if cfg is None:
        cfg = get_deye_cloud_config()
    target_sn = inverter_sn or cfg.get("inverter_sn", DEFAULT_INVERTER_SN)

    if len(slots) != 6:
        return {
            "success": False,
            "error": f"TOU schedule requires exactly 6 slots, got {len(slots)}",
        }

    formatted_slots = []
    for i, s in enumerate(slots):
        formatted_slots.append({
            "slot": i + 1,
            "time": s.get("time", "00:00"),
            "power": s.get("power_w", s.get("power", 5000)),
            "soc": s.get("target_soc", s.get("soc", 20)),
            "charge": bool(s.get("grid_charge", False)),
        })

    payload = {
        "deviceSn": target_sn,
        "energyPattern": "TOU",
        "slots": formatted_slots,
    }

    ok, res, msg = call_deye_api("/v1.0/order/sys/energyPattern/update", method="POST", data=payload, cfg=cfg)
    return {
        "success": ok,
        "source": "deye_cloud_openapi",
        "device_sn": target_sn,
        "slots_count": len(slots),
        "response": res,
        "message": msg,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deye Cloud OpenAPI Client for Home Assistant")
    parser.add_argument("--read-all", action="store_true", help="Read config, telemetry, and TOU settings via Cloud API")
    parser.add_argument("--read-soc", action="store_true", help="Read battery SOC via Cloud API")
    parser.add_argument("--test-auth", action="store_true", help="Test authentication and token acquisition")
    parser.add_argument("--get-token", action="store_true", help="Fetch and output raw access token (exchanging AppId/Secret/Email/Password)")
    parser.add_argument("--sync-tou", type=str, help="Synchronize 6 TOU slots (JSON string or file path)")
    parser.add_argument("--device-sn", default=None, help="Inverter Serial Number (defaults to DEYE_INVERTER_SN)")
    parser.add_argument("--api-key", default=None, help="Direct Deye API key override")

    args = parser.parse_args(argv)
    cfg = get_deye_cloud_config()
    if args.device_sn:
        cfg["inverter_sn"] = args.device_sn
    if args.api_key:
        cfg["api_key"] = args.api_key

    if args.get_token:
        token, status = get_access_token(cfg)
        if token:
            print(json.dumps({
                "success": True,
                "token": token,
                "status": status,
                "token_cache": DEFAULT_TOKEN_CACHE_FILE,
            }, indent=2))
            return 0
        else:
            print(json.dumps({
                "success": False,
                "error": status,
            }, indent=2))
            return 1

    if args.test_auth:
        token, status = get_access_token(cfg)
        print(json.dumps({
            "authenticated": token is not None,
            "status": status,
            "api_url": cfg["api_url"],
            "device_sn": cfg["inverter_sn"],
            "token_masked": f"{token[:8]}...{token[-4:]}" if token and len(token) > 12 else token,
        }, indent=2))
        return 0 if token else 1

    if args.read_all:
        res = read_inverter_telemetry(cfg=cfg)
        print(json.dumps(res, indent=2))
        return 0 if res["success"] else 1

    if args.read_soc:
        res = read_inverter_telemetry(cfg=cfg)
        soc = res["telemetry"]["battery_soc"]
        print(json.dumps({"success": res["success"], "battery_soc": soc}, indent=2))
        return 0 if res["success"] else 1

    if args.sync_tou:
        raw_val = args.sync_tou
        try:
            if raw_val.startswith("[") or raw_val.startswith("{"):
                slots_data = json.loads(raw_val)
            else:
                with open(raw_val, "r", encoding="utf-8") as f:
                    slots_data = json.load(f)
            if isinstance(slots_data, dict) and "slots" in slots_data:
                slots_data = slots_data["slots"]
            res = sync_tou_schedule(slots_data, cfg=cfg)
            print(json.dumps(res, indent=2))
            return 0 if res["success"] else 1
        except Exception as e:
            print(json.dumps({"success": False, "error": f"Failed to parse TOU slots: {e}"}, indent=2))
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
