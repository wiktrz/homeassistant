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


def test_read_inverter_telemetry_success():
    """Verifies telemetry reading from Deye Cloud API parses SOC, power, and voltage."""
    mock_data = {
        "soc": 88,
        "batteryVoltage": 52.1,
        "batteryPower": -2500,  # Charging 2.5 kW
        "batteryCurrent": 48.0,
        "activePower": 3200,
        "dailyYield": 14.5,
        "maxChargeSoc": 95,
        "minDischargeSoc": 20,
    }

    with mock.patch("deye_cloud_client.call_deye_api") as mock_api:
        mock_api.return_value = (True, {"data": mock_data}, "OK")
        cfg = {"inverter_sn": "2603160727", "api_key": "test"}
        res = deye_cloud_client.read_inverter_telemetry(cfg=cfg)

        assert res["success"] is True
        assert res["source"] == "deye_cloud_openapi"
        assert res["telemetry"]["battery_soc"] == 88
        assert res["telemetry"]["battery_voltage"] == 52.1
        assert res["telemetry"]["battery_power"] == -2500
        assert res["telemetry"]["pv_power"] == 3200
        assert res["telemetry"]["daily_yield"] == 14.5
        assert res["config"]["max_charge_soc"] == 95


def test_read_inverter_telemetry_missing_credentials_fallback():
    """Verifies read_inverter_telemetry fails gracefully with fallback structure when credentials missing."""
    cfg = {"api_key": None, "app_id": None, "email": None}
    res = deye_cloud_client.read_inverter_telemetry(cfg=cfg)
    assert res["success"] is False
    assert res["telemetry"]["battery_soc"] == 85
    assert res["config"]["max_charge_soc"] == 90
    assert "Missing credentials" in res["error"] or "Authentication error" in res["error"]


def test_sync_tou_schedule_cloud_payload():
    """Verifies sync_tou_schedule packages 6 slots and POSTs to energyPattern endpoint."""
    sample_slots = [
        {"time": "01:00", "power": 5000, "soc": 90, "grid_charge": True},
        {"time": "05:00", "power": 5000, "soc": 90, "grid_charge": False},
        {"time": "12:00", "power": 5000, "soc": 80, "grid_charge": False},
        {"time": "16:00", "power": 5000, "soc": 80, "grid_charge": False},
        {"time": "18:00", "power": 5000, "soc": 20, "grid_charge": False},
        {"time": "22:00", "power": 5000, "soc": 20, "grid_charge": False},
    ]

    with mock.patch("deye_cloud_client.call_deye_api") as mock_api:
        mock_api.return_value = (True, {"code": 0, "msg": "success"}, "OK")
        cfg = {"inverter_sn": "2603160727", "api_key": "test_key"}
        res = deye_cloud_client.sync_tou_schedule(sample_slots, cfg=cfg)

        assert res["success"] is True
        assert res["slots_count"] == 6
        assert mock_api.called
        endpoint, kwargs = mock_api.call_args[0][0], mock_api.call_args[1]
        assert "energyPattern" in endpoint
        assert kwargs["method"] == "POST"
        assert len(kwargs["data"]["slots"]) == 6


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
