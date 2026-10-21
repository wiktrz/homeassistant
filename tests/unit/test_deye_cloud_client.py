#!/usr/bin/env python3
"""
Unit Test Suite for Deye Cloud OpenAPI Client and Home Assistant Fallback Integration.
"""

import os
import sys
import json
import pytest
import unittest.mock as mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

import deye_cloud_client
import solarman_v5_client


def test_load_dotenv_parses_variables(tmp_path):
    """Verifies zero-dependency load_dotenv parses key-value pairs ignoring comments and quotes."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Test comment\n"
        "DEYE_TEST_VAR=hello_world\n"
        "DEYE_QUOTED_VAR='with_quotes'\n"
        "DEYE_DOUBLE_QUOTED=\"with_double\"\n"
        "\n"
        "# Empty line above\n"
    )

    with mock.patch.dict(os.environ, {}, clear=True):
        deye_cloud_client.load_dotenv(paths=[str(env_file)])
        assert os.environ.get("DEYE_TEST_VAR") == "hello_world"
        assert os.environ.get("DEYE_QUOTED_VAR") == "with_quotes"
        assert os.environ.get("DEYE_DOUBLE_QUOTED") == "with_double"


def test_get_deye_cloud_config_defaults_and_env():
    """Verifies default values and environment overrides for Deye Cloud config."""
    with mock.patch.dict(
        os.environ,
        {
            "DEYE_CLOUD_API_URL": "https://eu1-developer.deyecloud.com",
            "DEYE_CLOUD_API_KEY": "test_api_key_123",
            "DEYE_INVERTER_IP": "10.20.2.6",
            "DEYE_INVERTER_SN": "2603160727",
        },
    ):
        cfg = deye_cloud_client.get_deye_cloud_config()
        assert cfg["api_url"] == "https://eu1-developer.deyecloud.com"
        assert cfg["api_key"] == "test_api_key_123"
        assert cfg["inverter_ip"] == "10.20.2.6"
        assert cfg["inverter_sn"] == "2603160727"


def test_hash_deye_password():
    """Verifies SHA256 hashing for plain text passwords and normalization for existing hashes."""
    plain = "MySecretPassword123"
    hashed = deye_cloud_client.hash_deye_password(plain)
    assert len(hashed) == 64
    assert hashed == hashed.lower()
    # Hashing an already hashed string should preserve it
    assert deye_cloud_client.hash_deye_password(hashed) == hashed
    assert deye_cloud_client.hash_deye_password(None) is None


def test_get_access_token_with_direct_api_key():
    """Verifies that direct API key is used directly as token without HTTP call."""
    cfg = {"api_key": "my_super_secret_api_key"}
    token, status = deye_cloud_client.get_access_token(cfg)
    assert token == "my_super_secret_api_key"
    assert status == "api_key"


def test_get_access_token_via_oauth(tmp_path):
    """Verifies token retrieval via POST /v1.0/account/token?appId=... and disk caching."""
    cache_path = str(tmp_path / "token_cache.json")
    cfg = {
        "api_url": "https://eu1-developer.deyecloud.com",
        "app_id": "app_123",
        "app_secret": "sec_456",
        "email": "user@domain.com",
        "password": "plain_password",
        "api_key": None,
    }

    mock_resp = mock.MagicMock()
    mock_resp.read.return_value = json.dumps({
        "code": 1000000,
        "success": True,
        "data": {
            "accessToken": "tok_xyz_789",
            "expiresIn": 3600
        }
    }).encode()
    mock_resp.__enter__.return_value = mock_resp

    with mock.patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        token, status = deye_cloud_client.get_access_token(cfg, cache_file=cache_path)
        assert token == "tok_xyz_789"
        assert status == "new_token"

        # Verify request URL had ?appId=app_123 query parameter
        called_req = mock_urlopen.call_args[0][0]
        assert "appId=app_123" in called_req.full_url
        sent_body = json.loads(called_req.data.decode("utf-8"))
        assert sent_body["appSecret"] == "sec_456"
        assert sent_body["email"] == "user@domain.com"
        assert sent_body["password"] == deye_cloud_client.hash_deye_password("plain_password")

        # Verify cached on second call
        token2, status2 = deye_cloud_client.get_access_token(cfg, cache_file=cache_path)
        assert token2 == "tok_xyz_789"
        assert status2 == "cached"


def test_read_inverter_telemetry_success_with_battery():
    """Verifies telemetry reading from Deye Cloud API parses SOC, power, and voltage when battery installed."""
    mock_dev_resp = {
        "success": True,
        "deviceDataList": [
            {
                "deviceSn": "2603160727",
                "deviceState": 1,
                "dataList": [
                    {"key": "BatteryVoltage", "value": "52.1", "unit": "V"},
                    {"key": "SOC", "value": "88", "unit": "%"},
                    {"key": "BatteryPower", "value": "-2500", "unit": "W"},
                    {"key": "BatteryTotalCurrent", "value": "48.0", "unit": "A"},
                    {"key": "TotalSolarPower", "value": "3200", "unit": "W"},
                    {"key": "DailyActiveProduction", "value": "14.5", "unit": "kWh"},
                    {"key": "TotalGridPower", "value": "480", "unit": "W"},
                    {"key": "AC Temperature", "value": "34.5", "unit": "℃"},
                ]
            }
        ]
    }
    mock_bat_resp = {
        "success": True,
        "maxChargeSoc": 95,
        "minDischargeSoc": 20,
        "battLowCapacity": 20,
        "battShutDownCapacity": 5,
        "maxChargeCurrent": 100,
        "maxDischargeCurrent": 100,
    }
    mock_tou_resp = {
        "success": True,
        "touAction": "on",
        "timeUseSettingItems": [
            {"time": "0100", "power": 5000, "soc": 90, "enableGridCharge": True}
        ]
    }

    def fake_api(endpoint, method="POST", data=None, cfg=None):
        if "device/latest" in endpoint:
            return True, mock_dev_resp, "OK"
        elif "config/battery" in endpoint:
            return True, mock_bat_resp, "OK"
        elif "config/tou" in endpoint:
            return True, mock_tou_resp, "OK"
        return True, {}, "OK"

    with mock.patch("deye_cloud_client.call_deye_api", side_effect=fake_api):
        cfg = {"inverter_sn": "2603160727", "api_key": "test"}
        res = deye_cloud_client.read_inverter_telemetry(cfg=cfg)

        assert res["success"] is True
        assert res["source"] == "deye_cloud_openapi"
        assert res["telemetry"]["battery_installed"] is True
        assert res["telemetry"]["battery_soc"] == 88
        assert res["telemetry"]["battery_voltage"] == 52.1
        assert res["telemetry"]["battery_power"] == -2500
        assert res["telemetry"]["pv_power"] == 3200
        assert res["telemetry"]["grid_power"] == 480
        assert res["telemetry"]["temperature"] == 34.5
        assert res["config"]["min_discharge_soc"] == 20
        assert res["active_tou"]["enabled"] is True


def test_read_inverter_telemetry_no_battery_installed():
    """Verifies telemetry reading correctly detects pre-battery state (voltage < 40V, SOC 0%)."""
    mock_dev_resp = {
        "success": True,
        "deviceDataList": [
            {
                "deviceSn": "2603160727",
                "deviceState": 1,
                "dataList": [
                    {"key": "BatteryVoltage", "value": "8.89", "unit": "V"},
                    {"key": "SOC", "value": "0", "unit": "%"},
                    {"key": "BatteryPower", "value": "-1", "unit": "W"},
                    {"key": "TotalGridPower", "value": "482", "unit": "W"},
                    {"key": "TotalConsumptionPower", "value": "482", "unit": "W"},
                    {"key": "DailyConsumption", "value": "5.60", "unit": "kWh"},
                    {"key": "AC Temperature", "value": "32.30", "unit": "℃"},
                ]
            }
        ]
    }

    def fake_api(endpoint, method="POST", data=None, cfg=None):
        if "device/latest" in endpoint:
            return True, mock_dev_resp, "OK"
        return True, {}, "OK"

    with mock.patch("deye_cloud_client.call_deye_api", side_effect=fake_api):
        cfg = {"inverter_sn": "2603160727", "api_key": "test"}
        res = deye_cloud_client.read_inverter_telemetry(cfg=cfg)

        assert res["success"] is True
        assert res["telemetry"]["battery_installed"] is False
        assert res["telemetry"]["battery_soc"] == 0
        assert res["telemetry"]["battery_power"] == 0
        assert res["telemetry"]["grid_power"] == 482
        assert res["telemetry"]["consumption_power"] == 482
        assert res["telemetry"]["daily_consumption"] == 5.60
        assert res["telemetry"]["temperature"] == 32.30


def test_read_inverter_telemetry_missing_credentials_fallback():
    """Verifies read_inverter_telemetry fails gracefully with fallback structure when credentials missing."""
    cfg = {"api_key": None, "app_id": None, "email": None}
    res = deye_cloud_client.read_inverter_telemetry(cfg=cfg)
    assert res["success"] is False
    assert res["telemetry"]["battery_installed"] is False
    assert res["config"]["max_charge_soc"] == 90
    assert "Missing credentials" in res["error"] or "Authentication error" in res["error"]


def test_sync_tou_schedule_cloud_payload():
    """Verifies sync_tou_schedule packages 6 slots and POSTs to /v1.0/order/sys/tou/update endpoint."""
    sample_slots = [
        {"time": "01:00", "power": 5000, "soc": 90, "grid_charge": True},
        {"time": "05:00", "power": 5000, "soc": 90, "grid_charge": False},
        {"time": "12:00", "power": 5000, "soc": 80, "grid_charge": False},
        {"time": "16:00", "power": 5000, "soc": 80, "grid_charge": False},
        {"time": "18:00", "power": 5000, "soc": 20, "grid_charge": False},
        {"time": "22:00", "power": 5000, "soc": 20, "grid_charge": False},
    ]

    with mock.patch("deye_cloud_client.call_deye_api") as mock_api:
        mock_api.return_value = (True, {"code": "1106000", "msg": "order sent successfully", "success": True}, "OK")
        cfg = {"inverter_sn": "2603160727", "api_key": "test_key"}
        res = deye_cloud_client.sync_tou_schedule(sample_slots, cfg=cfg)

        assert res["success"] is True
        assert res["slots_count"] == 6
        assert res["message"] == "order sent successfully"
        assert mock_api.called
        endpoint, kwargs = mock_api.call_args[0][0], mock_api.call_args[1]
        assert "/v1.0/order/sys/tou/update" in endpoint
        assert kwargs["method"] == "POST"
        assert "touAction" not in kwargs["data"]
        assert kwargs["data"]["timeUseSettingItems"][0]["time"] == "01:00"
        assert isinstance(kwargs["data"]["timeUseSettingItems"][0]["voltage"], int)
        assert kwargs["data"]["timeUseSettingItems"][0]["enableGeneration"] is False
        assert kwargs["data"]["timeUseSettingItems"][0]["enableSell"] is False


def test_solarman_cloud_fallback_when_local_refused():
    """Verifies that solarman_v5_client automatically falls back to deye_cloud_client when local Modbus is refused."""
    # Mock local Modbus to fail (simulating blocked port 8899)
    with mock.patch("solarman_v5_client.read_holding_registers", return_value={"success": False, "registers": [], "message": "Connection refused"}):
        # Mock deye_cloud_client to return live telemetry
        mock_cloud_telem = {
            "success": True,
            "source": "deye_cloud_openapi",
            "telemetry": {"battery_soc": 89, "battery_voltage": 51.8, "battery_power": 0},
            "config": {"max_charge_soc": 90, "min_discharge_soc": 20},
            "active_tou": {"enabled": True, "slots": []},
        }
        with mock.patch("deye_cloud_client.get_deye_cloud_config", return_value={"api_key": "valid_key"}):
            with mock.patch("deye_cloud_client.read_inverter_telemetry", return_value=mock_cloud_telem):
                res = solarman_v5_client.read_inverter_telemetry("10.20.2.6", 8899)
                assert res["success"] is True
                assert res["source"] == "deye_cloud_openapi"
                assert res["telemetry"]["battery_soc"] == 89


def test_call_deye_api_normalizes_bearer_token():
    """Verifies that Authorization and x-deye-token headers correctly strip redundant Bearer prefixes."""
    cfg = {"api_url": "https://eu1-developer.deyecloud.com", "api_key": "Bearer eyJhbGciOiJSUzI1Ni..."}
    mock_resp = mock.MagicMock()
    mock_resp.read.return_value = json.dumps({"code": 1000000, "data": {"status": "ok"}}).encode()
    mock_resp.__enter__.return_value = mock_resp

    with mock.patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        ok, res, msg = deye_cloud_client.call_deye_api("/v1.0/test", cfg=cfg)
        assert ok is True
        called_req = mock_urlopen.call_args[0][0]
        assert called_req.headers["Authorization"] == "Bearer eyJhbGciOiJSUzI1Ni..."
        assert called_req.headers["X-deye-token"] == "eyJhbGciOiJSUzI1Ni..."


def test_get_access_token_missing_account_credentials():
    """Verifies clear error message when App ID/Secret provided without email/password."""
    cfg = {
        "api_url": "https://eu1-developer.deyecloud.com",
        "app_id": "app_123",
        "app_secret": "sec_456",
        "email": None,
        "password": None,
        "api_key": None,
    }
    token, status = deye_cloud_client.get_access_token(cfg)
    assert token is None
    assert "Missing account credentials" in status
    assert "DEYE_CLOUD_EMAIL" in status


def test_cli_sync_pstryk_tou_flag():
    """Verifies CLI flag --sync-pstryk-tou computes schedule and triggers cloud upload."""
    mock_pstryk = {
        "powerbank_best_window": {"start": "2026-10-08T02:00:00Z", "end": "2026-10-08T04:00:00Z"},
        "best_sell_window": {"start": "2026-10-08T17:00:00Z", "end": "2026-10-08T20:00:00Z"},
    }
    mock_telem = {
        "success": True,
        "config": {"max_charge_soc": 90, "min_discharge_soc": 20},
        "telemetry": {"battery_soc": 85},
    }
    with mock.patch("pstryk_engine.fetch_consolidated_data", return_value=mock_pstryk), \
         mock.patch("deye_cloud_client.read_inverter_telemetry", return_value=mock_telem), \
         mock.patch("deye_cloud_client.sync_tou_schedule", return_value={"success": True, "slots_count": 6}):
        ret = deye_cloud_client.main(["--sync-pstryk-tou"])
        assert ret == 0


def test_set_battery_safety_limits_cloud_success():
    """Verifies set_battery_safety_limits_cloud formats payload according to OpenAPI schema and customControl."""
    with mock.patch("deye_cloud_client.call_deye_api") as mock_api:
        mock_api.return_value = (True, {"code": "1000000", "msg": "success"}, "OK")
        cfg = {"inverter_sn": "2603160727", "api_key": "test_key"}
        res = deye_cloud_client.set_battery_safety_limits_cloud(min_soc=15, shutdown_soc=5, cfg=cfg)

        assert res["success"] is True
        assert res["min_soc"] == 15
        assert res["shutdown_soc"] == 5

        # Verify calls: 1st for BATT_LOW, 2nd for Shutdown SOC customControl
        assert mock_api.call_count == 2
        call_min = mock_api.call_args_list[0]
        assert call_min[0][0] == "/v1.0/order/battery/parameter/update"
        assert call_min[1]["data"]["deviceSn"] == "2603160727"
        assert call_min[1]["data"]["paramterType"] == "BATT_LOW"
        assert call_min[1]["data"]["value"] == 15

        call_shut = mock_api.call_args_list[1]
        assert call_shut[0][0] == "/v1.0/order/customControl"
        assert call_shut[1]["data"]["deviceSn"] == "2603160727"
        assert "content" in call_shut[1]["data"]


def test_set_charge_current_cloud_success():
    """Verifies set_charge_current_cloud formats payload according to OpenAPI schema."""
    with mock.patch("deye_cloud_client.call_deye_api") as mock_api:
        mock_api.return_value = (True, {"code": "1000000", "msg": "success"}, "OK")
        cfg = {"inverter_sn": "2603160727", "api_key": "test_key"}
        res = deye_cloud_client.set_charge_current_cloud(current_a=50, cfg=cfg)

        assert res["success"] is True
        assert res["current_a"] == 50
        mock_api.assert_called_once()
        args, kwargs = mock_api.call_args
        assert args[0] == "/v1.0/order/battery/parameter/update"
        assert kwargs["data"]["deviceSn"] == "2603160727"
        assert kwargs["data"]["paramterType"] == "MAX_CHARGE_CURRENT"
        assert kwargs["data"]["value"] == 50


def test_set_battery_safety_limits_cloud_validation():
    """Verifies set_battery_safety_limits_cloud rejects shutdown_soc >= min_soc."""
    cfg = {"inverter_sn": "2603160727", "api_key": "test_key"}
    res = deye_cloud_client.set_battery_safety_limits_cloud(min_soc=15, shutdown_soc=20, cfg=cfg)
    assert res["success"] is False
    assert "Invalid safety limits" in res["message"]


def test_cli_set_safety_limits_flag():
    """Verifies CLI flag --set-safety-limits calls set_battery_safety_limits_cloud."""
    with mock.patch("deye_cloud_client.set_battery_safety_limits_cloud") as mock_set:
        mock_set.return_value = {"success": True, "min_soc": 15, "shutdown_soc": 5}
        ret = deye_cloud_client.main(["--set-safety-limits", "--min-soc", "15", "--shutdown-soc", "5"])
        assert ret == 0
        mock_set.assert_called_once_with(min_soc=15, shutdown_soc=5, cfg=mock.ANY)





def test_cloud_tou_non_charging_mid_soc_slot_and_no_charge_current():
    """R1/R2: non-charging slot with 30<SOC<80 gets a label; TOU power is not reported as charge current."""
    tou = {"success": True, "data": {"touAction": "on", "timeUseSettingItems": [
        {"time": "0100", "power": 7000, "soc": 40, "enableGridCharge": False},
        {"time": "0500", "power": 7000, "soc": 90, "enableGridCharge": True},
    ]}}

    def fake_api(path, method="GET", data=None, cfg=None, **kw):
        return (True, tou, "OK") if path == "/v1.0/config/tou" else (False, {}, "skip")

    cfg = {"api_key": "k", "inverter_sn": "SN1"}
    with mock.patch("deye_cloud_client.call_deye_api", side_effect=fake_api), \
         mock.patch("deye_cloud_client.get_deye_cloud_token", return_value="t", create=True):
        res = deye_cloud_client.read_inverter_telemetry(cfg=cfg, force_refresh=True, cache_path=os.devnull)
    slots = res["active_tou"]["slots"]
    assert slots[0]["label"] == "Autokonsumpcja PV"
    assert all(s["charge_current_a"] is None for s in slots)


def _fake_clock(step_sleep=True):
    clock = {"t": 1000.0}
    return clock, mock.patch("solarman_v5_client.time.monotonic", side_effect=lambda: clock["t"]), \
        mock.patch("solarman_v5_client.time.sleep", side_effect=lambda x: clock.__setitem__("t", clock["t"] + x))


def test_slow_but_healthy_requests_never_start_deadline():
    """H2: successful-but-slow requests (30 x 3s) must not trip the budget."""
    clock, p_mono, p_sleep = _fake_clock()

    def slow_sock(*a, **kw):
        clock["t"] += 3.0
        m = mock.MagicMock()
        m.__enter__.return_value.recv.return_value = b"ok"
        return m

    with p_mono, p_sleep, mock.patch("solarman_v5_client.socket.socket", side_effect=slow_sock):
        for _ in range(30):
            ok, _, _ = solarman_v5_client.send_command("10.0.0.1", 8899, b"x")
            assert ok
    assert solarman_v5_client._deadline is None
    assert clock["t"] - 1000.0 >= 90


def test_first_failed_attempt_starts_deadline_then_fails_fast():
    import socket as _socket
    clock, p_mono, p_sleep = _fake_clock()
    attempts = []

    def fake_sock(*a, **kw):
        attempts.append(1)
        clock["t"] += 5.0
        raise _socket.timeout()

    with p_mono, p_sleep, mock.patch("solarman_v5_client.socket.socket", side_effect=fake_sock):
        ok, _, _ = solarman_v5_client.send_command("10.0.0.1", 8899, b"x", timeout=5, retries=8)
        assert not ok
        # started when the first attempt failed (t=1005), not at entry
        assert solarman_v5_client._deadline == 1005.0 + solarman_v5_client.LOCAL_TIME_BUDGET_S
        n = len(attempts)
        assert n < 9  # budget cut the retries short
        solarman_v5_client.set_deadline()  # later entry point must not extend it
        assert solarman_v5_client._deadline == 1030.0
        ok, _, _ = solarman_v5_client.send_command("10.0.0.1", 8899, b"x", timeout=5, retries=8)
        assert not ok and len(attempts) == n


def test_tou_sync_skips_local_writes_when_deadline_active():
    solarman_v5_client._deadline = 12345.0
    slots = [{"time": "01:00"}] * 6
    with mock.patch("solarman_v5_client.write_single_register") as m_w, \
         mock.patch("deye_cloud_client.get_deye_cloud_config", return_value={"api_key": "k"}), \
         mock.patch("deye_cloud_client.sync_tou_schedule", return_value={"success": True}) as m_cloud:
        res = solarman_v5_client.sync_tou_schedule(slots)
    m_w.assert_not_called()
    m_cloud.assert_called_once()
    assert res["success"] is True and res["cloud_attempted"] is True


def test_set_charge_current_cloud_idempotency_honours_ttl():
    """L1: cache lookup must not ignore TTL."""
    with mock.patch("solarman_v5_client.load_deye_cache", return_value=None) as m_cache, \
         mock.patch("deye_cloud_client.call_deye_api", return_value=(True, {"success": True}, "OK")):
        deye_cloud_client.set_charge_current_cloud(30, cfg={"inverter_sn": "SN1"})
    assert m_cache.call_args_list[0] == mock.call()


def test_solarman_failures_report_cloud_attempted():
    fail = {"success": False, "message": "down"}
    with mock.patch("solarman_v5_client.write_single_register", return_value=fail), \
         mock.patch("solarman_v5_client.load_deye_cache", return_value=None), \
         mock.patch("deye_cloud_client.set_charge_current_cloud", return_value={"success": False}):
        assert solarman_v5_client.set_charge_current(30)["cloud_attempted"] is True
    slots = [{"time": "01:00"}] * 6
    with mock.patch("solarman_v5_client.write_single_register", return_value=fail), \
         mock.patch("deye_cloud_client.get_deye_cloud_config", return_value={}):
        assert solarman_v5_client.sync_tou_schedule(slots)["cloud_attempted"] is False
    assert solarman_v5_client.sync_tou_schedule([])["cloud_attempted"] is False
