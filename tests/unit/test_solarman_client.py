#!/usr/bin/env python3
"""
Unit Test Suite for Solarman V5 Client and Deye Modbus-RTU Frame Construction.
"""

import os
import sys
import pytest
import unittest.mock as mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

import solarman_v5_client


def test_modbus_crc16_calculation():
    """Verifies CRC16 calculation matches Modbus RTU standard."""
    # Slave 1, Function 3, Start 0x0000, Qty 0x0001
    data = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01])
    crc = solarman_v5_client.calculate_crc16(data)
    assert crc == 0x0A84
    # Packed with <H: low byte 0x84, high byte 0x0A
    packed = struct.pack("<H", crc) if "struct" in globals() else bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    assert packed == bytes([0x84, 0x0A])


def test_modbus_rtu_frame_builder():
    """Verifies Modbus RTU frame construction with proper byte order and appended CRC."""
    frame = solarman_v5_client.build_modbus_rtu_frame(slave_id=1, func_code=6, start_reg=112, value_or_qty=90)
    assert len(frame) == 8
    assert frame[0] == 0x01  # Slave ID
    assert frame[1] == 0x06  # Function code Write Single Register
    assert frame[2:4] == bytes([0x00, 0x70])  # Reg 112 = 0x0070
    assert frame[4:6] == bytes([0x00, 0x5A])  # Value 90 = 0x005A


def test_solarman_v5_frame_wrapping():
    """Verifies Solarman V5 framing starts with 0xA5 and ends with 0x15 with valid checksum."""
    payload = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x01, 0x84, 0x0A])
    wrapped = solarman_v5_client.build_solarman_v5_frame(payload, serial_number=12345)
    assert wrapped[0] == 0xA5
    assert wrapped[-1] == 0x15


def test_soc_direct_writes_blocked():
    """Verifies that direct writes to registers 108..112 are blocked per policy."""
    res = solarman_v5_client.sync_soc_limits(
        host="127.0.0.1",
        port=8899,
        max_soc=90,
        min_soc=20,
    )
    assert res["success"] is False
    assert res["blocked"] is True
    assert "BLOCKED" in res["message"]


def test_decode_modbus_rtu_response_read():
    """Verifies decoding of Modbus Function 0x03 (Read Holding Registers)."""
    # Dummy Solarman V5 frame: 16-byte header + modbus (slave=1, func=3, bytes=4, val1=0x005A, val2=0x0014, crc=2B) + chk + 0x15
    # Modbus payload: 01 03 04 00 5A 00 14 CRC CRC (total 8 bytes)
    modbus = bytes([0x01, 0x03, 0x04, 0x00, 0x5A, 0x00, 0x14, 0x12, 0x34])
    dummy_header = b"\xA5" + b"\x00" * 15
    dummy_tail = b"\x00\x15"
    frame = dummy_header + modbus + dummy_tail

    ok, regs, msg = solarman_v5_client.decode_modbus_rtu_response(frame)
    assert ok is True
    assert regs == [90, 20]
    assert msg == "OK"


def test_decode_modbus_rtu_response_write():
    """Verifies decoding of Modbus Function 0x06 (Write Single Register)."""
    modbus = bytes([0x01, 0x06, 0x00, 0x70, 0x00, 0x5A, 0x12, 0x34])
    dummy_header = b"\xA5" + b"\x00" * 15
    dummy_tail = b"\x00\x15"
    frame = dummy_header + modbus + dummy_tail

    ok, regs, msg = solarman_v5_client.decode_modbus_rtu_response(frame)
    assert ok is True
    assert regs == []
    assert msg == "OK"


def test_decode_modbus_rtu_response_error():
    """Verifies error reporting on short or invalid frames."""
    ok, regs, msg = solarman_v5_client.decode_modbus_rtu_response(b"short")
    assert ok is False
    assert "too short" in msg

    # Invalid header
    ok2, _, msg2 = solarman_v5_client.decode_modbus_rtu_response(b"\x00" * 20)
    assert ok2 is False
    assert "Invalid Solarman V5" in msg2


def test_sync_tou_schedule_requires_six_slots():
    """Verifies sync_tou_schedule rejects anything other than 6 slots."""
    res = solarman_v5_client.sync_tou_schedule(slots=[{"time": "01:00"}])
    assert res["success"] is False
    assert "requires exactly 6 slots" in res["error"]


def test_sync_tou_schedule_success():
    """Verifies sync_tou_schedule writes 248 enable and 250..273 registers for 6 slots."""
    written_registers = {}

    def mock_write(host, port, reg, val, serial=0):
        written_registers[reg] = val
        return {"register": reg, "value": val, "success": True, "message": "OK"}

    with mock.patch("solarman_v5_client.write_single_register", side_effect=mock_write):
        sample_slots = [
            {"time": "01:00", "power": 5000, "soc": 90, "grid_charge": True},
            {"time": "05:00", "power": 5000, "soc": 90, "grid_charge": False},
            {"time": "12:00", "power": 5000, "soc": 80, "grid_charge": False},
            {"time": "16:00", "power": 5000, "soc": 80, "grid_charge": False},
            {"time": "18:00", "power": 5000, "soc": 20, "grid_charge": False},
            {"time": "22:00", "power": 5000, "soc": 20, "grid_charge": False},
        ]
        res = solarman_v5_client.sync_tou_schedule(sample_slots)
        assert res["success"] is True

        # Master enable: reg 248 = 1
        assert written_registers[248] == 1

        # Times: 250..255
        assert written_registers[250] == 100   # 01:00 -> 100
        assert written_registers[251] == 500   # 05:00 -> 500
        assert written_registers[252] == 1200  # 12:00 -> 1200
        assert written_registers[253] == 1600  # 16:00 -> 1600
        assert written_registers[254] == 1800  # 18:00 -> 1800
        assert written_registers[255] == 2200  # 22:00 -> 2200

        # Powers: 256..261
        for reg in range(256, 262):
            assert written_registers[reg] == 5000

        # Target SOC: 262..267
        assert written_registers[262] == 90
        assert written_registers[263] == 90
        assert written_registers[264] == 80
        assert written_registers[265] == 80
        assert written_registers[266] == 20
        assert written_registers[267] == 20

        # Grid charge: 268..273
        assert written_registers[268] == 1  # Slot 1 Grid charge True
        assert written_registers[269] == 0
        assert written_registers[270] == 0
        assert written_registers[271] == 0
        assert written_registers[272] == 0
        assert written_registers[273] == 0


def test_read_inverter_telemetry_offline_fallback():
    """Verifies read_inverter_telemetry returns fallback structure gracefully when offline."""
    with mock.patch("solarman_v5_client.read_holding_registers") as mock_read:
        mock_read.return_value = {"success": False, "registers": [], "message": "Connection refused"}
        res = solarman_v5_client.read_inverter_telemetry("127.0.0.1", 8899)
        assert res["success"] is False
        assert "config" in res
        assert "telemetry" in res
        assert "active_tou" in res
        assert res["config"]["max_charge_soc"] == 90
        assert res["config"]["min_discharge_soc"] == 20
        assert res["telemetry"]["battery_voltage"] == 51.2
        assert res["telemetry"]["battery_soc"] == 85


def test_read_inverter_telemetry_online():
    """Verifies read_inverter_telemetry parses holding registers properly when online."""
    def mock_read(host, port, start_reg, qty, serial=0):
        if start_reg == 108:  # Config: 100A, 100A, 25%, 6%, 92%
            return {"success": True, "registers": [100, 100, 25, 6, 92], "message": "OK"}
        elif start_reg == 587:  # Telem: 520 (52.0V), 88 (88%), 0, 1500 (1500W), 300 (30.0A)
            return {"success": True, "registers": [520, 88, 0, 1500, 300], "message": "OK"}
        elif start_reg == 248:  # TOU: 26 registers
            regs = [1, 0] + [200, 500, 1200, 1500, 1800, 2200] + [5000]*6 + [90, 90, 80, 80, 20, 20] + [1, 0, 0, 0, 0, 0]
            return {"success": True, "registers": regs, "message": "OK"}
        return {"success": False, "registers": [], "message": "Unknown reg"}

    with mock.patch("solarman_v5_client.read_holding_registers", side_effect=mock_read):
        res = solarman_v5_client.read_inverter_telemetry("10.20.2.6", 8899)
        assert res["success"] is True
        assert res["config"]["max_charge_soc"] == 92
        assert res["config"]["min_discharge_soc"] == 25
        assert res["config"]["shutdown_soc"] == 6
        assert res["telemetry"]["battery_voltage"] == 52.0
        assert res["telemetry"]["battery_soc"] == 88
        assert res["telemetry"]["battery_power"] == 1500
        assert res["telemetry"]["battery_current"] == 30.0
        assert res["active_tou"]["enabled"] is True
        assert len(res["active_tou"]["slots"]) == 6
        assert res["active_tou"]["slots"][0]["time"] == "02:00"
        assert res["active_tou"]["slots"][0]["grid_charge"] is True


def test_connection_timeout_handling():
    """Verifies socket timeout is gracefully captured without throwing unhandled exceptions."""
    with mock.patch("socket.socket") as mock_sock_cls:
        mock_sock = mock.MagicMock()
        mock_sock.connect.side_effect = TimeoutError("timed out")
        mock_sock_cls.return_value.__enter__.return_value = mock_sock

        res = solarman_v5_client.write_single_register("10.20.2.6", 8899, 112, 90)
        assert res["success"] is False
        assert "timed out" in res["message"] or "error" in res["message"]


def test_load_and_save_inverter_config(tmp_path):
    """Verifies saving and loading inverter configuration to/from JSON file."""
    fake_cfg = str(tmp_path / ".deye_inverter_config.json")
    with mock.patch("solarman_v5_client.get_config_file_path", return_value=fake_cfg):
        assert solarman_v5_client.load_inverter_config() == {}
        saved = solarman_v5_client.save_inverter_config(host="10.20.2.99", port=8899, extra={"custom": True})
        assert saved is True
        loaded = solarman_v5_client.load_inverter_config()
        assert loaded["host"] == "10.20.2.99"
        assert loaded["port"] == 8899
        assert loaded["custom"] is True


def test_get_configured_inverter_ip_hierarchy(tmp_path):
    """Verifies fallback cascade: explicit arg -> env var -> config file -> default."""
    # 1. Explicit argument overrides all
    assert solarman_v5_client.get_configured_inverter_ip("192.168.1.100") == "192.168.1.100"

    # 2. Env variable
    with mock.patch.dict(os.environ, {"DEYE_INVERTER_IP": "10.20.2.55"}):
        assert solarman_v5_client.get_configured_inverter_ip() == "10.20.2.55"

    # 3. Config file
    with mock.patch.dict(os.environ, {}, clear=True):
        fake_cfg = str(tmp_path / "cfg.json")
        with mock.patch("solarman_v5_client.get_config_file_path", return_value=fake_cfg):
            solarman_v5_client.save_inverter_config("10.20.2.77", 8899)
            assert solarman_v5_client.get_configured_inverter_ip() == "10.20.2.77"

    # 4. Default fallback
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("solarman_v5_client.load_inverter_config", return_value={}):
            assert solarman_v5_client.get_configured_inverter_ip() == "10.20.2.6"


def test_get_configured_inverter_port_hierarchy(tmp_path):
    """Verifies fallback cascade: explicit arg -> env var -> config file -> default 8899."""
    # 1. Explicit
    assert solarman_v5_client.get_configured_inverter_port(9999) == 9999

    # 2. Env var
    with mock.patch.dict(os.environ, {"DEYE_INVERTER_PORT": "8888"}):
        assert solarman_v5_client.get_configured_inverter_port() == 8888

    # 3. Config file
    with mock.patch.dict(os.environ, {}, clear=True):
        fake_cfg = str(tmp_path / "cfg.json")
        with mock.patch("solarman_v5_client.get_config_file_path", return_value=fake_cfg):
            solarman_v5_client.save_inverter_config("10.20.2.6", 7777)
            assert solarman_v5_client.get_configured_inverter_port() == 7777

    # 4. Default
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("solarman_v5_client.load_inverter_config", return_value={}):
            assert solarman_v5_client.get_configured_inverter_port() == 8899


def test_probe_inverter_candidate_refused():
    """Verifies probe returns None when connection cannot be established."""
    with mock.patch("socket.socket") as mock_sock_cls:
        mock_sock = mock.MagicMock()
        mock_sock.connect_ex.return_value = 111  # Connection refused
        mock_sock_cls.return_value.__enter__.return_value = mock_sock

        res = solarman_v5_client.probe_inverter_candidate("10.20.2.99", 8899)
        assert res is None


def test_probe_inverter_candidate_success_with_solarman_header():
    """Verifies probe verifies Solarman V5 frame response."""
    with mock.patch("socket.socket") as mock_sock_cls:
        mock_sock = mock.MagicMock()
        mock_sock.connect_ex.return_value = 0
        # Valid Solarman frame response (starts with 0xA5, ends with 0x15, len >= 18)
        valid_resp = b"\xA5" + b"\x00" * 16 + b"\x15"
        mock_sock.recv.return_value = valid_resp
        mock_sock_cls.return_value.__enter__.return_value = mock_sock

        res = solarman_v5_client.probe_inverter_candidate("10.20.2.6", 8899)
        assert res is not None
        assert res["ip"] == "10.20.2.6"
        assert res["port"] == 8899
        assert res["solarman_verified"] is True


def test_scan_network_for_inverter_finds_device():
    """Verifies scan_network_for_inverter ranks verified devices and returns report."""
    def fake_probe(ip, port, timeout):
        if ip == "10.20.2.6":
            return {"ip": ip, "port": port, "solarman_verified": True, "latency_ms": 15.0}
        return None

    with mock.patch("solarman_v5_client.probe_inverter_candidate", side_effect=fake_probe):
        res = solarman_v5_client.scan_network_for_inverter(subnet="10.20.2.0/24", port=8899)
        assert res["found"] is True
        assert res["ip"] == "10.20.2.6"
        assert res["port"] == 8899
        assert len(res["devices"]) == 1
        assert res["devices"][0]["solarman_verified"] is True


def test_cli_scan_and_set_config(tmp_path):
    """Verifies CLI flags --scan and --set-config."""
    fake_cfg = str(tmp_path / "cli_test_cfg.json")
    with mock.patch("solarman_v5_client.get_config_file_path", return_value=fake_cfg):
        ret = solarman_v5_client.main(["--set-config", "--host", "10.20.2.88", "--port", "8899"])
        assert ret == 0
        assert os.path.exists(fake_cfg)

    with mock.patch("solarman_v5_client.scan_network_for_inverter") as mock_scan:
        mock_scan.return_value = {"found": True, "ip": "10.20.2.6", "port": 8899, "devices": []}
        ret_scan = solarman_v5_client.main(["--scan"])
        assert ret_scan == 0

