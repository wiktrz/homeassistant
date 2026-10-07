#!/usr/bin/env python3
"""
Solarman V5 & Modbus-RTU Direct Socket Client for Deye 12kW Inverter.
Target Hardware: Deye SUN-12K-SG05LP3-EU-SM2 + SunDeposit 16.13 kWh LiFePO4.
Default IP: 10.20.2.6, Port: 8899.

Operating Mode:
- READ-ONLY for hardware settings (Registers 108..112, 587..591)
- WRITE-ONLY for Time-of-Use (TOU) schedules (Registers 248..273)
- Direct writing to registers 108..112 is strictly BLOCKED per policy.
"""

import sys
import os
import time
import socket
import struct
import argparse
import json
import concurrent.futures
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

DEFAULT_INVERTER_IP = "10.20.2.6"
DEFAULT_INVERTER_PORT = 8899
DEFAULT_TIMEOUT = 5.0
CONFIG_FILENAME = ".deye_inverter_config.json"


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


load_dotenv()


# Modbus Holding Registers (Deye Hybrid Inverter):
# 1. Config Registers (Read-Only)
REG_MAX_CHARGE_CURRENT = 108     # 0x006C (A)
REG_MAX_DISCHARGE_CURRENT = 109  # 0x006D (A)
REG_MIN_DISCHARGE_SOC = 110      # 0x006E (%) - Capacity Grid-tied lower bound
REG_SHUTDOWN_SOC = 111           # 0x006F (%) - Critical Blackout cut-off
REG_MAX_CHARGE_SOC = 112         # 0x0070 (%) - Upper charging limit

# 2. Live Telemetry Registers (Read-Only)
REG_BATTERY_VOLTAGE = 587        # 0x024B (0.1 V)
REG_BATTERY_SOC = 588            # 0x024C (%)
REG_BATTERY_POWER = 590          # 0x024E (W, signed 16-bit: positive=discharging, negative=charging)
REG_BATTERY_CURRENT = 591        # 0x024F (0.1 A, signed 16-bit)

# 3. Time of Use (TOU) Registers (Read / Write)
REG_TOU_ENABLE = 248             # 0x00F8 (1 = Enabled, 0 = Disabled)
REG_TOU_TIME_BASE = 250          # 250..255: Slot 1..6 Start Time (HH*100 + MM)
REG_TOU_POWER_BASE = 256         # 256..261: Slot 1..6 Power Limit (W)
REG_TOU_SOC_BASE = 262           # 262..267: Slot 1..6 Target SOC (%)
REG_TOU_CHARGE_BASE = 268        # 268..273: Slot 1..6 Grid Charge Flag (1 = On, 0 = Off)


def calculate_crc16(data: bytes) -> int:
    """Calculates standard Modbus RTU CRC16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_modbus_rtu_frame(slave_id: int, func_code: int, start_reg: int, value_or_qty: int) -> bytes:
    """Builds Modbus RTU payload with CRC16."""
    payload = struct.pack(">BBHH", slave_id, func_code, start_reg, value_or_qty)
    crc = calculate_crc16(payload)
    return payload + struct.pack("<H", crc)


def build_solarman_v5_frame(modbus_payload: bytes, serial_number: int = 0) -> bytes:
    """
    Wraps Modbus RTU payload in Solarman V5 frame:
    [0xA5][Length: 2B LE][Control: 0x1045][Seq: 2B][Serial: 4B LE][Payload][Checksum: 1B][0x15]
    """
    payload_len = len(modbus_payload)
    header = struct.pack("<BH HH I", 0xA5, payload_len + 15, 0x1045, 0x0001, serial_number)
    sub_header = b"\x02" + b"\x00" * 4
    full_body = header + sub_header + modbus_payload
    checksum = sum(full_body[1:]) & 0xFF
    return full_body + struct.pack("BB", checksum, 0x15)


def send_command(host: str, port: int, frame: bytes, timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, bytes, str]:
    """Sends raw frame to inverter stick logger and awaits response."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            s.sendall(frame)
            response = s.recv(1024)
            if not response:
                return False, b"", f"Empty response received from {host}:{port}"
            return True, response, "OK"
    except socket.timeout:
        return False, b"", f"Connection timed out ({timeout}s) to {host}:{port}"
    except ConnectionRefusedError:
        return False, b"", f"Connection refused by {host}:{port}"
    except OSError as e:
        return False, b"", f"Socket error connecting to {host}:{port}: {e}"


def decode_modbus_rtu_response(solarman_resp: bytes) -> Tuple[bool, List[int], str]:
    """
    Extracts Modbus RTU response data bytes from Solarman V5 frame.
    Returns (success, registers_list, message).
    """
    if len(solarman_resp) < 18:
        return False, [], "Response too short to contain valid Solarman frame"

    if solarman_resp[0] != 0xA5 or solarman_resp[-1] != 0x15:
        return False, [], "Invalid Solarman V5 frame delimiters"

    # Modbus RTU payload starts at index 16, ends 2 bytes before end
    modbus_payload = solarman_resp[16:-2]
    if len(modbus_payload) < 5:
        return False, [], "Modbus payload too short"

    slave_id = modbus_payload[0]
    func_code = modbus_payload[1]

    if func_code & 0x80:
        err_code = modbus_payload[2] if len(modbus_payload) > 2 else 0
        return False, [], f"Modbus exception code: 0x{err_code:02X}"

    if func_code == 3:  # Read Holding Registers
        byte_count = modbus_payload[2]
        data_bytes = modbus_payload[3:3 + byte_count]
        if len(data_bytes) != byte_count or byte_count % 2 != 0:
            return False, [], f"Invalid byte count {byte_count}"

        qty = byte_count // 2
        regs = list(struct.unpack(f">{qty}H", data_bytes))
        return True, regs, "OK"

    if func_code == 6:  # Write Single Register
        return True, [], "OK"

    return False, [], f"Unsupported function code: {func_code}"


def get_config_file_path() -> str:
    """Returns persistent config file path (preferring /config if in Home Assistant container)."""
    if os.path.isdir("/config"):
        return os.path.join("/config", CONFIG_FILENAME)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, CONFIG_FILENAME)


def load_inverter_config() -> Dict[str, Any]:
    """
    Loads saved inverter configuration.
    Falls back to inspecting Home Assistant .storage/core.restore_state if available.
    """
    cfg_path = get_config_file_path()
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Check Home Assistant restore_state if present
    candidates = [
        "/config/.storage/core.restore_state",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".storage/core.restore_state"),
    ]
    for sc in candidates:
        if os.path.exists(sc):
            try:
                with open(sc, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ip_val = None
                    port_val = None
                    for item in data.get("data", []):
                        eid = item.get("state", {}).get("entity_id")
                        if eid == "input_text.deye_inverter_ip":
                            ip_val = item.get("state", {}).get("state")
                        elif eid == "input_number.deye_inverter_port":
                            try:
                                port_val = int(float(item.get("state", {}).get("state")))
                            except (ValueError, TypeError):
                                pass
                    res: Dict[str, Any] = {}
                    if ip_val and ip_val not in ("unknown", "unavailable", "None", ""):
                        res["host"] = ip_val
                    if port_val:
                        res["port"] = port_val
                    if res:
                        return res
            except Exception:
                pass

    return {}


def save_inverter_config(host: str, port: int = DEFAULT_INVERTER_PORT, extra: Optional[Dict[str, Any]] = None) -> bool:
    """Saves inverter connection parameters to persistent JSON configuration file."""
    cfg_path = get_config_file_path()
    data: Dict[str, Any] = {
        "host": host,
        "port": port,
        "updated_at": datetime.now().isoformat(),
    }
    if extra:
        data.update(extra)
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Warning: Failed to save inverter config to {cfg_path}: {e}", file=sys.stderr)
        return False


def get_configured_inverter_ip(explicit_host: Optional[str] = None) -> str:
    """
    Resolves active inverter IP address in priority:
    1. Explicit CLI / function argument (if provided and not None/empty/'default')
    2. DEYE_INVERTER_IP environment variable
    3. Saved config file / Home Assistant restore_state
    4. DEFAULT_INVERTER_IP ('10.20.2.6')
    """
    if explicit_host and explicit_host not in ("default", "None", ""):
        return explicit_host
    env_ip = os.getenv("DEYE_INVERTER_IP")
    if env_ip:
        return env_ip
    cfg = load_inverter_config()
    if cfg.get("host"):
        return str(cfg["host"])
    return DEFAULT_INVERTER_IP


def get_configured_inverter_port(explicit_port: Optional[int] = None) -> int:
    """
    Resolves active inverter port in priority:
    1. Explicit CLI / function argument (if provided and > 0)
    2. DEYE_INVERTER_PORT environment variable
    3. Saved config file / Home Assistant restore_state
    4. DEFAULT_INVERTER_PORT (8899)
    """
    if explicit_port and explicit_port > 0:
        return explicit_port
    env_port = os.getenv("DEYE_INVERTER_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    cfg = load_inverter_config()
    if cfg.get("port"):
        try:
            return int(cfg["port"])
        except ValueError:
            pass
    return DEFAULT_INVERTER_PORT


def probe_inverter_candidate(ip: str, port: int = DEFAULT_INVERTER_PORT, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """
    Probes a single IP on the network for Solarman V5 logger:
    1. Tests TCP connection to port.
    2. Sends a safe read frame (Modbus Read Holding Register 112) wrapped in Solarman V5 frame.
    3. Verifies Solarman V5 response header (0xA5) and trailer (0x15).
    """
    start_time = time.time()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            if s.connect_ex((ip, port)) != 0:
                return None
            latency_ms = round((time.time() - start_time) * 1000, 1)

            # TCP port is open, now send Solarman V5 frame probe
            probe_cmd = build_modbus_rtu_frame(slave_id=1, func_code=3, start_reg=REG_MAX_CHARGE_SOC, value_or_qty=1)
            probe_frame = build_solarman_v5_frame(probe_cmd)
            s.sendall(probe_frame)
            try:
                resp = s.recv(1024)
                solarman_verified = len(resp) >= 18 and resp[0] == 0xA5 and resp[-1] == 0x15
            except Exception:
                solarman_verified = False

            return {
                "ip": ip,
                "port": port,
                "solarman_verified": solarman_verified,
                "latency_ms": latency_ms,
            }
    except Exception:
        return None


def scan_network_for_inverter(
    subnet: Optional[str] = None,
    port: int = DEFAULT_INVERTER_PORT,
    timeout: float = 1.0,
    max_workers: int = 50,
) -> Dict[str, Any]:
    """
    Scans a local /24 subnet concurrently to discover Deye inverters listening on port 8899.
    Returns JSON discovery report.
    """
    start_all = time.time()

    # Determine subnet base
    if not subnet:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("1.1.1.1", 80))
                local_ip = s.getsockname()[0]
                parts = local_ip.split(".")
                if len(parts) == 4:
                    subnet = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        except Exception:
            subnet = "10.20.2.0/24"

    if not subnet:
        subnet = "10.20.2.0/24"

    # Extract base prefix (e.g. "10.20.2")
    raw_sub = subnet.split("/")[0]
    parts = raw_sub.split(".")
    base_prefix = f"{parts[0]}.{parts[1]}.{parts[2]}" if len(parts) >= 3 else "10.20.2"

    configured_ip = get_configured_inverter_ip()
    ip_list: List[str] = []
    if configured_ip.startswith(base_prefix):
        ip_list.append(configured_ip)

    for i in range(1, 255):
        cand = f"{base_prefix}.{i}"
        if cand not in ip_list:
            ip_list.append(cand)

    candidates: List[Dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ip = {executor.submit(probe_inverter_candidate, ip, port, timeout): ip for ip in ip_list}
        for future in concurrent.futures.as_completed(future_to_ip):
            res = future.result()
            if res:
                candidates.append(res)

    candidates.sort(key=lambda x: (not x.get("solarman_verified", False), x.get("latency_ms", 99999)))

    duration = round(time.time() - start_all, 2)
    found = len(candidates) > 0
    best_candidate = candidates[0] if candidates else None

    return {
        "found": found,
        "ip": best_candidate["ip"] if best_candidate else None,
        "port": best_candidate["port"] if best_candidate else port,
        "scanned_subnet": f"{base_prefix}.0/24",
        "devices": candidates,
        "duration_seconds": duration,
        "message": f"Found {len(candidates)} candidate(s) on {base_prefix}.0/24" if found else f"No Solarman devices found on {base_prefix}.0/24",
    }


def write_single_register(host: str, port: int, register: int, value: int, serial: int = 0) -> Dict[str, Any]:
    """Writes a single Modbus holding register (Function 0x06)."""
    modbus_cmd = build_modbus_rtu_frame(slave_id=1, func_code=6, start_reg=register, value_or_qty=value)
    solarman_frame = build_solarman_v5_frame(modbus_cmd, serial_number=serial)
    success, resp, msg = send_command(host, port, solarman_frame)
    if success:
        dec_ok, _, dec_msg = decode_modbus_rtu_response(resp)
        if not dec_ok:
            return {"register": register, "value": value, "success": False, "message": dec_msg}
    return {
        "register": register,
        "value": value,
        "success": success,
        "response_hex": resp.hex() if resp else "",
        "message": msg,
    }


def read_holding_registers(host: str, port: int, start_reg: int, qty: int, serial: int = 0) -> Dict[str, Any]:
    """Reads Modbus holding registers (Function 0x03)."""
    modbus_cmd = build_modbus_rtu_frame(slave_id=1, func_code=3, start_reg=start_reg, value_or_qty=qty)
    solarman_frame = build_solarman_v5_frame(modbus_cmd, serial_number=serial)
    success, resp, msg = send_command(host, port, solarman_frame)
    if not success:
        return {"start_register": start_reg, "quantity": qty, "success": False, "registers": [], "message": msg}

    dec_ok, regs, dec_msg = decode_modbus_rtu_response(resp)
    return {
        "start_register": start_reg,
        "quantity": qty,
        "success": dec_ok,
        "registers": regs,
        "message": dec_msg,
        "response_hex": resp.hex() if resp else "",
    }


def sync_soc_limits(
    host: Optional[str] = None,
    port: Optional[int] = None,
    max_soc: Optional[int] = None,
    min_soc: Optional[int] = None,
    shutdown_soc: Optional[int] = None,
    charge_current: Optional[int] = None,
    discharge_current: Optional[int] = None,
) -> Dict[str, Any]:
    """
    STRICTLY BLOCKED: Direct write to registers 108..112 is disabled per policy.
    Registers 108..112 must be configured manually on the physical inverter screen.
    Home Assistant operates in Read-Only mode for these parameters.
    """
    host = get_configured_inverter_ip(host)
    port = get_configured_inverter_port(port)
    return {
        "success": False,
        "blocked": True,
        "message": "BLOCKED: Direct writing to registers 108..112 is disabled. Set parameters on the inverter physical screen; Home Assistant reads them automatically.",
        "host": host,
        "port": port,
        "registers": {},
    }


def read_inverter_telemetry(host: Optional[str] = None, port: Optional[int] = None) -> Dict[str, Any]:
    """
    Polls Deye inverter in Read-Only mode to retrieve config, telemetry, and active TOU table.
    """
    host = get_configured_inverter_ip(host)
    port = get_configured_inverter_port(port)
    timestamp = datetime.now().isoformat()

    # 1. Read Hardware Configuration (Registers 108..112)
    cfg_res = read_holding_registers(host, port, REG_MAX_CHARGE_CURRENT, 5)
    config = {
        "max_charge_current": 100,
        "max_discharge_current": 100,
        "min_discharge_soc": 20,
        "shutdown_soc": 5,
        "max_charge_soc": 90,
    }
    if cfg_res["success"] and len(cfg_res["registers"]) >= 5:
        r = cfg_res["registers"]
        config["max_charge_current"] = r[0]
        config["max_discharge_current"] = r[1]
        config["min_discharge_soc"] = r[2]
        config["shutdown_soc"] = r[3]
        config["max_charge_soc"] = r[4]

    # 2. Read Live Battery Telemetry (Registers 587..591)
    telem_res = read_holding_registers(host, port, REG_BATTERY_VOLTAGE, 5)
    telemetry = {
        "battery_voltage": 51.2,
        "battery_soc": 85,
        "battery_power": 0,
        "battery_current": 0.0,
    }
    if telem_res["success"] and len(telem_res["registers"]) >= 5:
        r = telem_res["registers"]
        telemetry["battery_voltage"] = round(r[0] * 0.1, 1)
        telemetry["battery_soc"] = r[1]
        # power (signed 16-bit at index 3: offset 590 - 587 = 3)
        p_raw = r[3]
        telemetry["battery_power"] = p_raw if p_raw < 32768 else p_raw - 65536
        # current (signed 16-bit at index 4: offset 591 - 587 = 4)
        c_raw = r[4]
        c_signed = c_raw if c_raw < 32768 else c_raw - 65536
        telemetry["battery_current"] = round(c_signed * 0.1, 1)

    # 3. Read Active TOU Schedule (Registers 248..273)
    tou_res = read_holding_registers(host, port, REG_TOU_ENABLE, 26)
    active_tou = {
        "enabled": False,
        "slots": [],
    }
    if tou_res["success"] and len(tou_res["registers"]) >= 26:
        r = tou_res["registers"]
        active_tou["enabled"] = bool(r[0])
        # r[0] = 248 (enable), r[1] = 249
        # r[2..7] = 250..255 (times)
        # r[8..13] = 256..261 (powers)
        # r[14..19] = 262..267 (socs)
        # r[20..25] = 268..273 (charges)
        for i in range(6):
            raw_time = r[2 + i]
            hh = raw_time // 100
            mm = raw_time % 100
            time_str = f"{hh:02d}:{mm:02d}"
            power = r[8 + i]
            soc = r[14 + i]
            charge = bool(r[20 + i] & 0x01)
            active_tou["slots"].append({
                "slot": i + 1,
                "time": time_str,
                "power_w": power,
                "target_soc": soc,
                "grid_charge": charge,
            })

    overall_success = cfg_res["success"] or telem_res["success"]

    if not overall_success:
        try:
            import deye_cloud_client
            cloud_cfg = deye_cloud_client.get_deye_cloud_config()
            if cloud_cfg.get("api_key") or cloud_cfg.get("app_id") or cloud_cfg.get("email"):
                cloud_res = deye_cloud_client.read_inverter_telemetry(cfg=cloud_cfg)
                if cloud_res.get("success"):
                    return cloud_res
        except Exception:
            pass

    return {
        "success": overall_success,
        "timestamp": timestamp,
        "host": host,
        "port": port,
        "config": config,
        "telemetry": telemetry,
        "active_tou": active_tou,
        "error": None if overall_success else cfg_res.get("message", "Connection failed"),
    }


def sync_tou_schedule(
    slots: List[Dict[str, Any]],
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Programs the 6-slot Time-of-Use schedule into the Deye inverter:
    - Master Enable (Reg 248 = 1)
    - Slot 1..6 Start Times (Regs 250..255)
    - Slot 1..6 Power Limits (Regs 256..261)
    - Slot 1..6 Target SOCs (Regs 262..267)
    - Slot 1..6 Grid Charge flags (Regs 268..273)
    """
    host = get_configured_inverter_ip(host)
    port = get_configured_inverter_port(port)

    if len(slots) != 6:
        return {
            "success": False,
            "error": f"TOU schedule requires exactly 6 slots, got {len(slots)}",
        }

    results = {}
    overall_success = True

    # 1. Enable TOU master switch
    res_en = write_single_register(host, port, REG_TOU_ENABLE, 1)
    results["tou_enable"] = res_en
    if not res_en["success"]:
        overall_success = False

    # 2. Write each of the 6 slots
    for i, slot in enumerate(slots):
        time_str = slot.get("time", "00:00")
        parts = time_str.split(":")
        hh = int(parts[0]) if len(parts) > 0 else 0
        mm = int(parts[1]) if len(parts) > 1 else 0
        encoded_time = (hh * 100) + mm

        power = int(slot.get("power_w", slot.get("power", 5000)))
        target_soc = int(slot.get("target_soc", slot.get("soc", 20)))
        target_soc = max(10, min(100, target_soc))

        grid_charge = 1 if slot.get("grid_charge", False) else 0

        # Reg 250+i: Time
        r_time = write_single_register(host, port, REG_TOU_TIME_BASE + i, encoded_time)
        # Reg 256+i: Power
        r_pwr = write_single_register(host, port, REG_TOU_POWER_BASE + i, power)
        # Reg 262+i: Target SOC
        r_soc = write_single_register(host, port, REG_TOU_SOC_BASE + i, target_soc)
        # Reg 268+i: Grid Charge
        r_chg = write_single_register(host, port, REG_TOU_CHARGE_BASE + i, grid_charge)

        results[f"slot_{i+1}"] = {
            "time": r_time,
            "power": r_pwr,
            "target_soc": r_soc,
            "grid_charge": r_chg,
        }

        if not all((r_time["success"], r_pwr["success"], r_soc["success"], r_chg["success"])):
            overall_success = False

    if not overall_success:
        try:
            import deye_cloud_client
            cloud_cfg = deye_cloud_client.get_deye_cloud_config()
            if cloud_cfg.get("api_key") or cloud_cfg.get("app_id") or cloud_cfg.get("email"):
                cloud_res = deye_cloud_client.sync_tou_schedule(slots, cfg=cloud_cfg)
                if cloud_res.get("success"):
                    return cloud_res
        except Exception:
            pass

    return {
        "success": overall_success,
        "host": host,
        "port": port,
        "slots_count": len(slots),
        "results": results,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Solarman V5 Deye Inverter Register Client")
    parser.add_argument("--host", default=None, help="Inverter IP address (defaults to configured/discovered IP or 10.20.2.6)")
    parser.add_argument("--port", type=int, default=None, help="Inverter port (defaults to configured port or 8899)")
    parser.add_argument("--read-all", action="store_true", help="Read config, telemetry, and TOU settings")
    parser.add_argument("--read-soc", action="store_true", help="Read battery SOC from register 588")
    parser.add_argument("--read-tou", action="store_true", help="Read current TOU schedule from inverter")
    parser.add_argument("--sync-tou", type=str, help="Synchronize 6 TOU slots (JSON string or file path)")
    # Scanner and Config persistence arguments
    parser.add_argument("--scan", action="store_true", help="Scan local network for Deye Solarman V5 inverters on port 8899")
    parser.add_argument("--subnet", type=str, default=None, help="Subnet to scan (e.g. 10.20.2.0/24)")
    parser.add_argument("--save-discovered", action="store_true", help="Automatically save discovered inverter IP to persistent config")
    parser.add_argument("--set-config", action="store_true", help="Persist specified --host and --port to configuration file")
    # Blocked write arguments
    parser.add_argument("--max-soc", type=int, help="Max Charge SOC (BLOCKED: read-only)")
    parser.add_argument("--min-soc", type=int, help="Min Discharge SOC (BLOCKED: read-only)")
    parser.add_argument("--shutdown-soc", type=int, help="Shutdown SOC (BLOCKED: read-only)")
    parser.add_argument("--charge-current", type=int, help="Max Charge Current (BLOCKED: read-only)")
    parser.add_argument("--discharge-current", type=int, help="Max Discharge Current (BLOCKED: read-only)")

    args = parser.parse_args(argv)

    if args.set_config:
        resolved_host = args.host or DEFAULT_INVERTER_IP
        resolved_port = args.port or DEFAULT_INVERTER_PORT
        saved = save_inverter_config(resolved_host, resolved_port)
        print(json.dumps({
            "success": saved,
            "host": resolved_host,
            "port": resolved_port,
            "config_file": get_config_file_path(),
        }, indent=2))
        return 0 if saved else 1

    if args.scan:
        scan_port = args.port or DEFAULT_INVERTER_PORT
        res = scan_network_for_inverter(subnet=args.subnet, port=scan_port)
        if args.save_discovered and res.get("found") and res.get("ip"):
            save_inverter_config(res["ip"], res["port"], extra={"discovered": True})
            res["config_saved"] = True
        print(json.dumps(res, indent=2))
        return 0

    resolved_host = get_configured_inverter_ip(args.host)
    resolved_port = get_configured_inverter_port(args.port)

    if any(val is not None for val in (args.max_soc, args.min_soc, args.shutdown_soc, args.charge_current, args.discharge_current)):
        res = sync_soc_limits(host=resolved_host, port=resolved_port)
        print(json.dumps(res, indent=2))
        return 1

    if args.read_all:
        res = read_inverter_telemetry(resolved_host, resolved_port)
        print(json.dumps(res, indent=2))
        return 0

    if args.read_soc:
        res = read_holding_registers(resolved_host, resolved_port, REG_BATTERY_SOC, 1)
        soc_val = res["registers"][0] if res["success"] and res["registers"] else 85
        print(json.dumps({"success": res["success"], "battery_soc": soc_val}, indent=2))
        return 0 if res["success"] else 1

    if args.read_tou:
        telem = read_inverter_telemetry(resolved_host, resolved_port)
        print(json.dumps(telem.get("active_tou", {}), indent=2))
        return 0 if telem["success"] else 1

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
            res = sync_tou_schedule(slots_data, host=resolved_host, port=resolved_port)
            print(json.dumps(res, indent=2))
            return 0 if res["success"] else 1
        except Exception as e:
            print(json.dumps({"success": False, "error": f"Failed to parse TOU slots: {e}"}, indent=2))
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
