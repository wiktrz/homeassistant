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
    Uses official POST endpoints (/v1.0/device/latest, /v1.0/config/battery, /v1.0/config/tou).
    Supports installations without PV or battery (e.g. pre-installation phase).
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
        "batt_capacity": 200,
    }
    telemetry_data = {
        "battery_voltage": 0.0,
        "battery_soc": 0,
        "battery_power": 0,
        "battery_current": 0.0,
        "battery_installed": False,
        "bms_version": "0000",
        "pv_power": 0,
        "daily_yield": 0.0,
        "total_yield": 0.0,
        "grid_power": 0,
        "grid_voltage_l1": 240.0,
        "grid_voltage_l2": 240.0,
        "grid_voltage_l3": 240.0,
        "grid_current_l1": 0.0,
        "grid_current_l2": 0.0,
        "grid_current_l3": 0.0,
        "grid_frequency": 50.0,
        "consumption_power": 0,
        "daily_consumption": 0.0,
        "daily_energy_purchased": 0.0,
        "total_energy_buy": 0.0,
        "total_energy_sell": 0.0,
        "temperature": 25.0,
        "device_state": 1,
    }
    active_tou = {
        "enabled": False,
        "slots": [],
    }

    cloud_ok = False
    last_err = "No endpoints answered"

    # 1. Telemetry Query via POST /v1.0/device/latest
    ok_dev, res_dev, msg_dev = call_deye_api(
        "/v1.0/device/latest",
        method="POST",
        data={"deviceList": [target_sn]},
        cfg=cfg,
    )
    if ok_dev and isinstance(res_dev, dict) and res_dev.get("success", True):
        cloud_ok = True
        dev_list = res_dev.get("deviceDataList", [])
        if dev_list and isinstance(dev_list, list):
            item = dev_list[0]
            telemetry_data["device_state"] = item.get("deviceState", 1)
            raw_data_list = item.get("dataList", [])
            # Map key-value pairs
            kv: Dict[str, Any] = {}
            for entry in raw_data_list:
                k = entry.get("key")
                v = entry.get("value")
                if k is not None:
                    kv[k] = v

            # Grid & Household loads
            if "TotalGridPower" in kv:
                try: telemetry_data["grid_power"] = int(float(kv["TotalGridPower"]))
                except (ValueError, TypeError): pass
            if "GridVoltageL1" in kv:
                try: telemetry_data["grid_voltage_l1"] = float(kv["GridVoltageL1"])
                except (ValueError, TypeError): pass
            if "GridVoltageL2" in kv:
                try: telemetry_data["grid_voltage_l2"] = float(kv["GridVoltageL2"])
                except (ValueError, TypeError): pass
            if "GridVoltageL3" in kv:
                try: telemetry_data["grid_voltage_l3"] = float(kv["GridVoltageL3"])
                except (ValueError, TypeError): pass
            if "GridCurrentL1" in kv:
                try: telemetry_data["grid_current_l1"] = float(kv["GridCurrentL1"])
                except (ValueError, TypeError): pass
            if "GridCurrentL2" in kv:
                try: telemetry_data["grid_current_l2"] = float(kv["GridCurrentL2"])
                except (ValueError, TypeError): pass
            if "GridCurrentL3" in kv:
                try: telemetry_data["grid_current_l3"] = float(kv["GridCurrentL3"])
                except (ValueError, TypeError): pass
            if "GridFrequency" in kv:
                try: telemetry_data["grid_frequency"] = float(kv["GridFrequency"])
                except (ValueError, TypeError): pass

            if "TotalConsumptionPower" in kv:
                try: telemetry_data["consumption_power"] = int(float(kv["TotalConsumptionPower"]))
                except (ValueError, TypeError): pass
            if "DailyConsumption" in kv:
                try: telemetry_data["daily_consumption"] = float(kv["DailyConsumption"])
                except (ValueError, TypeError): pass
            if "DailyEnergyPurchased" in kv:
                try: telemetry_data["daily_energy_purchased"] = float(kv["DailyEnergyPurchased"])
                except (ValueError, TypeError): pass
            if "TotalEnergyBuy" in kv:
                try: telemetry_data["total_energy_buy"] = float(kv["TotalEnergyBuy"])
                except (ValueError, TypeError): pass
            if "TotalEnergySell" in kv:
                try: telemetry_data["total_energy_sell"] = float(kv["TotalEnergySell"])
                except (ValueError, TypeError): pass

            # Solar PV
            for k_pv in ("TotalSolarPower", "activePower", "currentPower", "pvPower"):
                if k_pv in kv:
                    try:
                        telemetry_data["pv_power"] = int(float(kv[k_pv]))
                        break
                    except (ValueError, TypeError): pass
            for k_dy in ("DailyActiveProduction", "PVDailyPowerGenerationActive", "dailyYield", "yieldToday"):
                if k_dy in kv:
                    try:
                        telemetry_data["daily_yield"] = float(kv[k_dy])
                        break
                    except (ValueError, TypeError): pass
            for k_ty in ("TotalActiveProduction", "totalYield"):
                if k_ty in kv:
                    try:
                        telemetry_data["total_yield"] = float(kv[k_ty])
                        break
                    except (ValueError, TypeError): pass

            # Inverter Health & Thermal
            for k_tmp in ("AC Temperature", "Temperature- Inverter", "temperature"):
                if k_tmp in kv:
                    try:
                        telemetry_data["temperature"] = float(kv[k_tmp])
                        break
                    except (ValueError, TypeError): pass

            # Battery & BMS
            if "BatteryVoltage" in kv or "batteryVoltage" in kv or "vBat" in kv:
                raw_v = kv.get("BatteryVoltage", kv.get("batteryVoltage", kv.get("vBat")))
                try: telemetry_data["battery_voltage"] = float(raw_v)
                except (ValueError, TypeError): pass
            if "SOC" in kv or "soc" in kv or "batterySoc" in kv:
                raw_s = kv.get("SOC", kv.get("soc", kv.get("batterySoc")))
                try: telemetry_data["battery_soc"] = int(float(raw_s))
                except (ValueError, TypeError): pass
            if "BatteryPower" in kv or "batteryPower" in kv or "pBat" in kv:
                raw_p = kv.get("BatteryPower", kv.get("batteryPower", kv.get("pBat")))
                try: telemetry_data["battery_power"] = int(float(raw_p))
                except (ValueError, TypeError): pass
            if "BatteryTotalCurrent" in kv or "BatteryCurrent1" in kv or "batteryCurrent" in kv or "iBat" in kv:
                raw_c = kv.get("BatteryTotalCurrent", kv.get("BatteryCurrent1", kv.get("batteryCurrent", kv.get("iBat"))))
                try: telemetry_data["battery_current"] = float(raw_c)
                except (ValueError, TypeError): pass
            if "LithiumBatteryVersionNumber" in kv:
                telemetry_data["bms_version"] = str(kv["LithiumBatteryVersionNumber"])

            # Detect whether physical battery is actually installed
            # A real 48V/51.2V LiFePO4 battery pack exhibits voltage > 40V and non-zero SOC or communicating BMS
            v_bat = telemetry_data["battery_voltage"]
            soc_bat = telemetry_data["battery_soc"]
            bms_ver = telemetry_data["bms_version"]
            battery_installed = bool(v_bat > 40.0 and (soc_bat > 0 or (bms_ver and bms_ver != "0000")))
            telemetry_data["battery_installed"] = battery_installed

            if not battery_installed:
                telemetry_data["battery_soc"] = 0
                telemetry_data["battery_power"] = 0
                telemetry_data["battery_current"] = 0.0
    else:
        last_err = msg_dev

    # 2. Battery Config Query via POST /v1.0/config/battery
    ok_bat, res_bat, _ = call_deye_api(
        "/v1.0/config/battery",
        method="POST",
        data={"deviceSn": target_sn},
        cfg=cfg,
    )
    if ok_bat and isinstance(res_bat, dict) and res_bat.get("success", True):
        cloud_ok = True
        payload_bat = res_bat.get("data", res_bat)
        if "maxChargeCurrent" in payload_bat:
            try: config_data["max_charge_current"] = int(payload_bat["maxChargeCurrent"])
            except (ValueError, TypeError): pass
        if "maxDischargeCurrent" in payload_bat:
            try: config_data["max_discharge_current"] = int(payload_bat["maxDischargeCurrent"])
            except (ValueError, TypeError): pass
        if "battLowCapacity" in payload_bat:
            try: config_data["min_discharge_soc"] = int(payload_bat["battLowCapacity"])
            except (ValueError, TypeError): pass
        if "battShutDownCapacity" in payload_bat:
            try: config_data["shutdown_soc"] = int(payload_bat["battShutDownCapacity"])
            except (ValueError, TypeError): pass
        if "battCapacity" in payload_bat:
            try: config_data["batt_capacity"] = int(payload_bat["battCapacity"])
            except (ValueError, TypeError): pass

    # 3. TOU Schedule Query via POST /v1.0/config/tou
    ok_tou, res_tou, _ = call_deye_api(
        "/v1.0/config/tou",
        method="POST",
        data={"deviceSn": target_sn},
        cfg=cfg,
    )
    if ok_tou and isinstance(res_tou, dict) and res_tou.get("success", True):
        cloud_ok = True
        payload_tou = res_tou.get("data", res_tou)
        active_tou["enabled"] = (str(payload_tou.get("touAction", "on")).lower() in ("on", "true", "1"))
        raw_items = payload_tou.get("timeUseSettingItems", [])
        active_tou["slots"] = []
        for i, itm in enumerate(raw_items):
            t_raw = str(itm.get("time", "00:00"))
            if len(t_raw) == 4 and ":" not in t_raw:
                time_str = f"{t_raw[:2]}:{t_raw[2:]}"
            else:
                time_str = t_raw
            charge = bool(itm.get("enableGridCharge", False))
            soc = itm.get("soc", 20)
            if charge:
                label = "Najtańsze Ładowanie"
            elif soc >= 80:
                label = "Czuwanie Przed Szczytem (Hold)"
            elif soc <= 30:
                label = "Szczyt Cenowy (Autokonsumpcja)"
            else:
                label = "Autokonsumpcja PV"
            active_tou["slots"].append({
                "slot": i + 1,
                "time": time_str,
                "power_w": itm.get("power", 5000),
                "target_soc": soc,
                "grid_charge": charge,
                "label": label,
            })

    # 4. System Mode Query via POST /v1.0/config/system
    ok_sys, res_sys, _ = call_deye_api(
        "/v1.0/config/system",
        method="POST",
        data={"deviceSn": target_sn},
        cfg=cfg,
    )
    if ok_sys and isinstance(res_sys, dict) and res_sys.get("success", True):
        payload_sys = res_sys.get("data", res_sys)
        config_data["energy_pattern"] = payload_sys.get("energyPattern", "BATTERY_FIRST")
        config_data["system_work_mode"] = payload_sys.get("systemWorkMode", "ZERO_EXPORT_TO_CT")
        if "maxSolarPower" in payload_sys:
            config_data["max_solar_power"] = payload_sys.get("maxSolarPower")
        if "zeroExportPower" in payload_sys:
            config_data["zero_export_power"] = payload_sys.get("zeroExportPower")

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
    Endpoint: POST /v1.0/order/sys/tou/update
    """
    if cfg is None:
        cfg = get_deye_cloud_config()
    target_sn = inverter_sn or cfg.get("inverter_sn", DEFAULT_INVERTER_SN)

    if len(slots) != 6:
        return {
            "success": False,
            "error": f"TOU schedule requires exactly 6 slots, got {len(slots)}",
        }

    formatted_items = []
    for s in slots:
        raw_t = str(s.get("time", "00:00")).strip()
        if ":" in raw_t:
            parts = raw_t.split(":")
            formatted_time = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        elif len(raw_t) == 4 and raw_t.isdigit():
            formatted_time = f"{raw_t[:2]}:{raw_t[2:]}"
        elif len(raw_t) == 3 and raw_t.isdigit():
            formatted_time = f"0{raw_t[0]}:{raw_t[1:]}"
        else:
            formatted_time = "00:00"

        formatted_items.append({
            "time": formatted_time,
            "power": int(s.get("power_w", s.get("power", 5000))),
            "soc": int(s.get("target_soc", s.get("soc", 20))),
            "enableGridCharge": bool(s.get("grid_charge", False)),
            "enableGeneration": False,
            "enableSell": False,
            "voltage": int(s.get("voltage", 49)),
        })

    payload = {
        "deviceSn": target_sn,
        "timeUseSettingItems": formatted_items,
    }

    ok, res, msg = call_deye_api("/v1.0/order/sys/tou/update", method="POST", data=payload, cfg=cfg)
    app_success = False
    if ok and isinstance(res, dict):
        code = str(res.get("code", ""))
        app_success = (code in ("1000000", "1106000") or res.get("success") is True)
        if "msg" in res:
            msg = res.get("msg")
    elif ok:
        app_success = True

    return {
        "success": app_success,
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
    parser.add_argument("--sync-pstryk-tou", action="store_true", help="Calculate optimal TOU from Pstryk prices and upload via Cloud API")
    parser.add_argument("--device-sn", default=None, help="Inverter Serial Number (defaults to DEYE_INVERTER_SN)")
    parser.add_argument("--api-key", default=None, help="Direct Deye API key override")

    args = parser.parse_args(argv)
    cfg = get_deye_cloud_config()
    if args.device_sn:
        cfg["inverter_sn"] = args.device_sn
    if args.api_key:
        cfg["api_key"] = args.api_key

    if args.sync_pstryk_tou:
        try:
            import pstryk_engine
            pstryk_data = pstryk_engine.fetch_consolidated_data(installation="dol")
            pb_win = pstryk_data.get("powerbank_best_window")
            sell_win = pstryk_data.get("best_sell_window")

            inv_telem = read_inverter_telemetry(cfg=cfg)
            cfg_data = inv_telem.get("config", {})
            tlm_data = inv_telem.get("telemetry", {})

            slots = pstryk_engine.generate_deye_tou_schedule(
                current_soc=tlm_data.get("battery_soc", 85),
                max_soc=cfg_data.get("max_charge_soc", 90),
                min_soc=cfg_data.get("min_discharge_soc", 20),
                best_pb_window=pb_win,
                best_sell_window=sell_win,
            )

            res = sync_tou_schedule(slots, cfg=cfg)
            res["generated_slots"] = slots
            print(json.dumps(res, indent=2))
            return 0 if res.get("success") else 1
        except Exception as e:
            print(json.dumps({"success": False, "error": f"Failed to sync Pstryk TOU via Cloud API: {e}"}, indent=2))
            return 1

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
