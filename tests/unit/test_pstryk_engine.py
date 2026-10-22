#!/usr/bin/env python3
"""
Unit Test Suite for Pstryk Dynamic Energy Engine.
Tests API payload parsing, open-frame null safety, cheapest window algorithm,
cache lifecycle, aggregations, prosumer price derivation, HTTP error resilience,
and CLI interface.
"""

import os
import sys
import io
import time
import json
import pytest
import unittest.mock as mock
import urllib.error
from datetime import datetime, timedelta, timezone

# Ensure config/ directory is importable
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "pstryk")

if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

import pstryk_engine
import energy_defaults


@pytest.fixture(autouse=True)
def _hermetic_io(monkeypatch, tmp_path):
    """No test may reach the real inverter (Modbus) or Deye Cloud, or write runtime files into config/."""
    import socket

    def _blocked(*args, **kwargs):
        raise OSError("network access disabled in unit tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(pstryk_engine, "LAST_TOU_SYNC_PATHS", [str(tmp_path / ".deye_tou_last_sync.json")])
    monkeypatch.setenv("DEYE_CACHE_FILE", str(tmp_path / ".deye_inverter_cache.json"))
    # The TOU floors are projected from "now" (current_soc is valid at now_dt): pin the engine clock to today's
    # Warsaw midnight so schedule expectations do not depend on the hour the suite runs (freeze_now overrides it).
    today = datetime.now(pstryk_engine.WARSAW_TZ)
    freeze_now(monkeypatch, datetime(today.year, today.month, today.day, tzinfo=pstryk_engine.WARSAW_TZ))


WARSAW = pstryk_engine.WARSAW_TZ


def freeze_now(monkeypatch, aware_dt):
    """Freezes pstryk_engine's datetime.now() at aware_dt."""
    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return aware_dt.astimezone(tz) if tz else aware_dt.astimezone().replace(tzinfo=None)
    monkeypatch.setattr(pstryk_engine, "datetime", _FixedDT)


def warsaw_day_frames(day, price_fn=lambda h: 0.5, skip_hours=()):
    """Hourly pricing frames covering one full Warsaw calendar day (23/24/25 h across DST)."""
    t = datetime(day.year, day.month, day.day, tzinfo=WARSAW).astimezone(timezone.utc)
    nd = day + timedelta(days=1)
    end = datetime(nd.year, nd.month, nd.day, tzinfo=WARSAW).astimezone(timezone.utc)
    frames = []
    while t < end:
        h = t.astimezone(WARSAW).hour
        if h not in skip_hours:
            p = price_fn(h)
            frames.append({
                "start": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": (t + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "metrics": {"pricing": {"price_gross": p, "price_net": round(p / 1.23, 4), "price_prosumer_gross": p}},
            })
        t += timedelta(hours=1)
    return frames


def warsaw_today_iso():
    return datetime.now(WARSAW).date().isoformat()


def slot_minutes(slots):
    return [int(s["time"][:2]) * 60 + int(s["time"][3:]) for s in slots]


def load_fixture(name: str) -> dict:
    fixture_path = os.path.join(FIXTURES_DIR, name)
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_api_payload_parsing_all_metrics():
    """Verifies parsing of all 4 metrics: pricing, cost, meter_values, and carbon."""
    latest_payload = load_fixture("sample_latest.json")
    parsed_frames = pstryk_engine.parse_metrics(latest_payload)

    assert len(parsed_frames) == 1, "Should parse exactly 1 frame from sample_latest.json"
    frame = parsed_frames[0]

    # 1. Pricing metric
    assert frame["pricing"]["price_net"] == 0.4521
    assert frame["pricing"]["price_gross"] == 0.5561
    assert frame["pricing"]["sell_price_net"] == 0.3812
    assert frame["pricing"]["sell_price_gross"] == 0.4689

    # 2. Cost metric
    assert frame["cost"]["cost_net"] == 1.3563
    assert frame["cost"]["cost_gross"] == 1.6683
    assert frame["cost"]["sell_net"] == 0.3812
    assert frame["cost"]["sell_gross"] == 0.4689
    assert frame["cost"]["balance"] == 1.1994

    # 3. Meter values metric
    assert frame["meter_values"]["a_plus"] == 3.0
    assert frame["meter_values"]["a_minus"] == 1.0

    # 4. Carbon metric
    assert frame["carbon"]["co2_eq"] == 1850.0
    assert frame["carbon"]["factor"] == 616.67


def test_open_frame_null_safety():
    """
    Ensures is_live: true open frames with null values (meter_values: null, cost: null)
    do not raise TypeError or NoneType formatting errors.
    """
    hourly_payload = load_fixture("sample_hourly_today.json")
    parsed_frames = pstryk_engine.parse_metrics(hourly_payload)

    # Frame at index 10 represents the open live hour
    live_frame = parsed_frames[10]
    assert live_frame["is_live"] is True
    assert live_frame["pricing"]["price_gross"] == 0.5000
    assert live_frame["cost"] is None
    assert live_frame["meter_values"] is None
    assert live_frame["carbon"] is None

    # Null-safety test: performing aggregations or string conversions must not raise TypeError
    try:
        aggregates = pstryk_engine.aggregate_frames(hourly_payload)
        assert isinstance(aggregates, dict)
        assert "total_kwh_bought" in aggregates

        # String formatting null check
        formatted_mv = f"Consumption: {live_frame['meter_values']}"
        assert formatted_mv == "Consumption: None"
    except TypeError as e:
        pytest.fail(f"Open frame with null metrics raised TypeError: {e}")


def test_cheapest_window_expansion_algorithm():
    """
    Verifies that the window expands forward and backward from the minimum price
    as long as price rise between consecutive hours is <= 10%.
    """
    hourly_payload = load_fixture("sample_hourly_today.json")
    result = pstryk_engine.find_cheapest_window(hourly_payload)

    assert "error" not in result, f"Algorithm returned error: {result.get('error')}"

    # In sample_hourly_today.json:
    # Hour 13: 0.30 (min)
    # Hour 12: 0.32 (<= 0.30 * 1.10 = 0.33) -> included
    # Hour 11: 0.40 (> 0.32 * 1.10 = 0.352) -> stopped backward expansion
    # Hour 14: 0.31 (<= 0.30 * 1.10 = 0.33) -> included
    # Hour 15: 0.33 (<= 0.31 * 1.10 = 0.341) -> included
    # Hour 16: 0.45 (> 0.33 * 1.10 = 0.363) -> stopped forward expansion
    # Window spans hours 12:00 to 16:00 (4 hours)
    assert result["start"] == "2026-09-24T12:00:00Z"
    assert result["end"] == "2026-09-24T16:00:00Z"
    assert result["duration_hours"] == 4

    assert result["gross"]["min"] == 0.30
    assert result["gross"]["max"] == 0.33
    assert result["gross"]["avg"] == round((0.32 + 0.30 + 0.31 + 0.33) / 4, 5)


def test_cache_lifecycle_read_write_ttl(tmp_path):
    """
    Verifies file caching in /tmp (or custom tmp_path):
    1. Fresh cache hit within 15 min TTL bypasses HTTP request.
    2. Expired cache (> 15 min) triggers refresh.
    3. Corrupted cache is safely discarded.
    """
    cache_dir = str(tmp_path)
    cache_file = pstryk_engine.get_cache_file_path("dol", cache_dir=cache_dir)
    mock_payload = {"frames": [{"test": "ok", "metrics": {}}]}

    # 1. Write cache
    ok = pstryk_engine.write_cache(cache_file, mock_payload)
    assert ok is True
    assert os.path.exists(cache_file)

    # 2. Read fresh cache (< 15 min TTL)
    cached = pstryk_engine.read_cache(cache_file, ttl_seconds=900)
    assert cached is not None
    assert cached.get("frames") == [{"test": "ok", "metrics": {}}]

    # Cache hit in get_pstryk_data should not trigger urllib.request.urlopen
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        hit_data = pstryk_engine.get_pstryk_data(
            api_key="sk-DUMMY",
            installation="dol",
            use_cache=True,
            cache_ttl=900,
            cache_dir=cache_dir,
        )
        assert hit_data.get("_cache_hit") is True
        mock_urlopen.assert_not_called()

    # 3. Expired cache handling (> 15 min TTL)
    # Artificially age the cache file by 1000 seconds
    past_time = time.time() - 1000
    os.utime(cache_file, (past_time, past_time))

    expired = pstryk_engine.read_cache(cache_file, ttl_seconds=900)
    assert expired is None, "Expired cache must return None"

    # 4. Corrupted cache handling
    with open(cache_file, "w", encoding="utf-8") as f:
        f.write("{invalid json content 123#$!@")

    corrupted = pstryk_engine.read_cache(cache_file, ttl_seconds=900)
    assert corrupted is None, "Corrupted JSON cache must safely return None"


def test_daily_and_monthly_aggregations():
    """
    Verifies summation of kWh and PLN matching summary totals across
    daily (month-to-date) and monthly (year-to-date) datasets.
    """
    daily_payload = load_fixture("sample_daily_month.json")
    daily_summary = daily_payload.get("summary", {})
    daily_aggs = pstryk_engine.aggregate_frames(daily_payload)

    assert daily_aggs["total_kwh_bought"] == daily_summary["total_kwh_bought"] == 360.0
    assert daily_aggs["total_kwh_sold"] == daily_summary["total_kwh_sold"] == 120.0
    assert daily_aggs["total_cost_pln"] == daily_summary["total_cost_pln"] == 180.0
    assert daily_aggs["total_earned_pln"] == daily_summary["total_earned_pln"] == 45.0
    assert daily_aggs["balance_pln"] == daily_summary["balance_pln"] == 135.0

    monthly_payload = load_fixture("sample_monthly_year.json")
    monthly_summary = monthly_payload.get("summary", {})
    monthly_aggs = pstryk_engine.aggregate_frames(monthly_payload)

    assert monthly_aggs["total_kwh_bought"] == monthly_summary["total_kwh_bought"] == 2380.0
    assert monthly_aggs["total_kwh_sold"] == monthly_summary["total_kwh_sold"] == 1570.0
    assert monthly_aggs["total_cost_pln"] == monthly_summary["total_cost_pln"] == 1392.0
    assert monthly_aggs["total_earned_pln"] == monthly_summary["total_earned_pln"] == 625.0
    assert monthly_aggs["balance_pln"] == monthly_summary["balance_pln"] == 767.0


def test_prosumer_selling_price_derivation():
    """Verifies prosumer gross and net extraction and VAT derivation."""
    latest_payload = load_fixture("sample_latest.json")
    prosumer_data = pstryk_engine.extract_prosumer_pricing(latest_payload)

    assert len(prosumer_data) == 1
    p = prosumer_data[0]
    assert p["sell_price_net"] == 0.3812
    assert p["sell_price_gross"] == 0.4689

    # Test derivation when sell_price_gross is missing from payload
    synthetic_payload = {
        "frames": [
            {
                "start": "2026-09-24T12:00:00Z",
                "end": "2026-09-24T13:00:00Z",
                "metrics": {
                    "pricing": {
                        "price_net": 0.50,
                        "sell_price_net": 0.40,
                        "sell_price_gross": None,
                    }
                },
            }
        ]
    }
    derived = pstryk_engine.extract_prosumer_pricing(synthetic_payload)
    assert derived[0]["sell_price_net"] == 0.40
    # 0.40 * 1.23 = 0.4920
    assert derived[0]["sell_price_gross"] == 0.4920


def test_network_and_http_error_resilience(tmp_path):
    """Tests mock HTTP 400, 401, 500 and timeout handling without crashing."""
    cache_dir = str(tmp_path)

    # 1. HTTP 400 Bad Request / Invalid API Key
    err_400_fp = io.BytesIO(b'{"error": "Invalid API key"}')
    mock_400 = urllib.error.HTTPError(
        url="https://api.pstryk.pl",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=err_400_fp,
    )
    with mock.patch("urllib.request.urlopen", side_effect=mock_400):
        res = pstryk_engine.get_pstryk_data(
            api_key="sk-INVALID",
            use_cache=False,
            cache_dir=cache_dir,
        )
        assert res.get("status_code") == 400
        assert "Invalid API key" in res.get("error", "")

    # 2. HTTP 401 Unauthorized
    err_401_fp = io.BytesIO(b'{"detail": "Authentication credentials were not provided."}')
    mock_401 = urllib.error.HTTPError(
        url="https://api.pstryk.pl",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=err_401_fp,
    )
    with mock.patch("urllib.request.urlopen", side_effect=mock_401):
        res = pstryk_engine.get_pstryk_data(
            api_key="sk-WRONG",
            use_cache=False,
            cache_dir=cache_dir,
        )
        assert res.get("status_code") == 401
        assert "Authentication" in res.get("error", "")

    # 3. HTTP 500 Internal Server Error
    mock_500 = urllib.error.HTTPError(
        url="https://api.pstryk.pl",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=None,
    )
    with mock.patch("urllib.request.urlopen", side_effect=mock_500):
        res = pstryk_engine.get_pstryk_data(
            api_key="sk-VALID",
            use_cache=False,
            cache_dir=cache_dir,
        )
        assert res.get("status_code") == 500
        assert "HTTP Error 500" in res.get("error", "")

    # 4. Connection Timeout / URLError
    mock_timeout = urllib.error.URLError("Connection timed out")
    with mock.patch("urllib.request.urlopen", side_effect=mock_timeout):
        res = pstryk_engine.get_pstryk_data(
            api_key="sk-VALID",
            use_cache=False,
            cache_dir=cache_dir,
        )
        assert "error" in res
        assert "Connection timed out" in res["error"]


def test_cli_argument_parsing_and_output(capsys):
    """Tests CLI arguments --key, --hours, --json, and --installation dol|gora."""
    hourly_payload = load_fixture("sample_hourly_today.json")

    mock_resp = mock.MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(hourly_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with mock.patch("urllib.request.urlopen", return_value=mock_resp):
        # 1. Test installation dol
        ret = pstryk_engine.main([
            "--key", "sk-TEST-KEY-DOL",
            "--hours", "24",
            "--json",
            "--installation", "dol",
            "--no-cache",
        ])
        assert ret == 0
        captured = capsys.readouterr()
        out_json = json.loads(captured.out)
        assert out_json["installation"] == "dol"
        assert out_json["duration_hours"] == 4

        # 2. Test installation gora
        ret = pstryk_engine.main([
            "--key", "sk-TEST-KEY-GORA",
            "--hours", "24",
            "--json",
            "--installation", "gora",
            "--no-cache",
        ])
        assert ret == 0
        captured = capsys.readouterr()
        out_json = json.loads(captured.out)
        assert out_json["installation"] == "gora"
        assert out_json["duration_hours"] == 4


def test_consolidated_payload_structure(tmp_path):
    """
    Verifies that fetch_consolidated_data generates a complete, backward-compatible,
    and rich JSON payload with all required top-level keys and structured sub-objects:
    current, today, month, and year.
    """
    cache_dir = str(tmp_path)
    latest_payload = load_fixture("sample_latest.json")
    hourly_payload = load_fixture("sample_hourly_today.json")
    daily_payload = load_fixture("sample_daily_month.json")
    monthly_payload = load_fixture("sample_monthly_year.json")

    def mock_fetch_api(url, params, api_key, timeout=10):
        if params.get("temporal") == "latest":
            return latest_payload
        if params.get("resolution") == "month":
            return monthly_payload
        if params.get("resolution") == "day":
            return daily_payload
        return hourly_payload

    with mock.patch("pstryk_engine.fetch_api", side_effect=mock_fetch_api):
        consolidated = pstryk_engine.fetch_consolidated_data(
            api_key="sk-CONSOLIDATED-TEST",
            installation="dol",
            use_cache=False,
            cache_dir=cache_dir,
        )

        # 1. Top-level backward compatibility
        assert isinstance(consolidated["current_price"], float)
        assert consolidated["start"] == "2026-09-24T12:00:00Z"
        assert consolidated["end"] == "2026-09-24T16:00:00Z"
        assert consolidated["duration_hours"] == 4
        assert isinstance(consolidated["all_prices"], list)
        assert len(consolidated["all_prices"]) > 0
        assert "min" in consolidated["net"] and "max" in consolidated["net"] and "avg" in consolidated["net"]
        assert "min" in consolidated["gross"] and "max" in consolidated["gross"] and "avg" in consolidated["gross"]
        assert consolidated["installation"] == "dol"

        # 2. 'current' sub-object
        curr = consolidated["current"]
        assert curr["price_gross"] == 0.5561
        assert curr["price_net"] == 0.4521
        assert curr["sell_price_gross"] == 0.4689
        assert curr["sell_price_net"] == 0.3812
        assert curr["tge_price"] == 0.4521
        assert curr["dist_price"] == 0.0
        assert curr["service_price"] == 0.0
        assert curr["vat"] == 0.104
        assert curr["is_cheap"] is False
        assert curr["is_expensive"] is False
        assert curr["instant_kwh_import"] == 3.0
        assert curr["carbon_g"] == 1850.0

        # 3. 'today' sub-object
        today = consolidated["today"]
        assert today["kwh_import"] == 24.0
        assert today["kwh_export"] == 3.0
        assert today["cost_pln"] == 14.13
        assert today["earned_pln"] == 1.65
        assert today["balance_pln"] == 12.48
        assert today["carbon_g"] == 14690.0
        assert isinstance(today["hourly"], list)
        assert today["hourly"][0]["start"] == "02:00"
        assert today["hourly"][0]["end"] == "03:00"
        assert today["hourly"][0]["kwh"] == 2.0
        assert today["hourly"][0]["cost_pln"] == 1.0
        assert today["hourly"][0]["rate_gross"] == 0.5

        # 4. 'month' sub-object
        month = consolidated["month"]
        assert month["kwh_import"] == 360.0
        assert month["kwh_export"] == 120.0
        assert month["cost_pln"] == 180.0
        assert month["earned_pln"] == 45.0
        assert month["balance_pln"] == 135.0
        assert isinstance(month["daily"], list)
        assert len(month["daily"]) == 30
        assert month["daily"][0]["date"] == "2026-09-01"
        assert month["daily"][0]["kwh"] == 12.0
        assert month["daily"][0]["cost_pln"] == 6.0
        assert month["daily"][0]["avg_rate"] == 0.5

        # 5. 'year' sub-object
        year = consolidated["year"]
        assert isinstance(year["monthly"], list)
        assert len(year["monthly"]) == 7
        assert year["monthly"][0]["month"] == "2026-03"
        assert year["monthly"][0]["kwh"] == 450.0
        assert year["monthly"][0]["cost_pln"] == 270.0


def test_consolidated_null_safety_and_open_frames():
    """
    Asserts that open frames with null metrics in today, month, or year
    do not raise exceptions and yield safe float defaults.
    """
    empty_data = {"frames": None}
    assert pstryk_engine.build_current_subobject(empty_data)["price_gross"] == 0.0
    assert pstryk_engine.build_today_subobject(empty_data)["kwh_import"] == 0.0
    assert pstryk_engine.build_month_subobject(empty_data)["cost_pln"] == 0.0
    assert pstryk_engine.build_year_subobject(empty_data)["monthly"] == []

    open_frame_payload = {
        "frames": [
            {
                "start": "2026-09-24T12:00:00Z",
                "end": "2026-09-24T13:00:00Z",
                "is_live": True,
                "metrics": {
                    "pricing": {"price_gross": None, "price_net": None},
                    "cost": None,
                    "meter_values": None,
                    "carbon": None,
                },
            }
        ]
    }
    curr = pstryk_engine.build_current_subobject(open_frame_payload)
    assert curr["price_gross"] == 0.0
    assert curr["price_net"] == 0.0
    assert curr["instant_kwh_import"] == 0.0
    assert curr["carbon_g"] == 0.0

    today = pstryk_engine.build_today_subobject(open_frame_payload)
    assert today["kwh_import"] == 0.0
    assert today["cost_pln"] == 0.0
    assert len(today["hourly"]) == 1
    assert today["hourly"][0]["kwh"] == 0.0
    assert today["hourly"][0]["cost_pln"] == 0.0

    month = pstryk_engine.build_month_subobject(open_frame_payload)
    assert month["kwh_import"] == 0.0
    assert month["cost_pln"] == 0.0
    assert len(month["daily"]) == 1
    assert month["daily"][0]["kwh"] == 0.0

    year = pstryk_engine.build_year_subobject(open_frame_payload)
    assert len(year["monthly"]) == 1
    assert year["monthly"][0]["kwh"] == 0.0


def test_consolidated_smart_cache_lifecycle(tmp_path, monkeypatch):
    """
    Verifies smart file caching in /tmp/pstryk_cache_{installation}.json:
    - 15-minute TTL cache hit returns instantly without API requests.
    - Cache expiration (> 15 min) triggers a refresh.
    - Corrupt cache file is safely removed and refreshed.
    """
    cache_dir = str(tmp_path)
    cache_file = pstryk_engine.get_cache_file_path("gora", cache_dir=cache_dir)
    # Full Warsaw-day price coverage: degraded (partial) payloads are intentionally never cached
    freeze_now(monkeypatch, datetime(2026, 9, 24, 12, 0, tzinfo=WARSAW))
    day = datetime(2026, 9, 24).date()
    hourly_payload = {"frames": warsaw_day_frames(day) + warsaw_day_frames(day + timedelta(days=1))}

    mock_resp = mock.MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(hourly_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with mock.patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        # First call: cache miss, triggers HTTP calls and writes cache
        res1 = pstryk_engine.fetch_consolidated_data(
            api_key="sk-CACHE-TEST",
            installation="gora",
            use_cache=True,
            cache_dir=cache_dir,
        )
        assert os.path.exists(cache_file)
        assert res1["installation"] == "gora"
        assert res1.get("_cache_hit") is not True
        initial_calls = mock_urlopen.call_count
        assert initial_calls > 0

        # Second call: cache hit (< 15 min TTL), no new HTTP calls
        res2 = pstryk_engine.fetch_consolidated_data(
            api_key="sk-CACHE-TEST",
            installation="gora",
            use_cache=True,
            cache_dir=cache_dir,
        )
        assert res2.get("_cache_hit") is True
        assert mock_urlopen.call_count == initial_calls

        # Age the cache file beyond 15-minute TTL (> 900 seconds)
        past_time = time.time() - 1000
        os.utime(cache_file, (past_time, past_time))

        res3 = pstryk_engine.fetch_consolidated_data(
            api_key="sk-CACHE-TEST",
            installation="gora",
            use_cache=True,
            cache_dir=cache_dir,
        )
        assert res3.get("_cache_hit") is not True
        assert mock_urlopen.call_count > initial_calls

        # Corrupt the cache file
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write("{corrupt-json-12345")

        res4 = pstryk_engine.fetch_consolidated_data(
            api_key="sk-CACHE-TEST",
            installation="gora",
            use_cache=True,
            cache_dir=cache_dir,
        )
        assert res4["installation"] == "gora"
        assert res4.get("_cache_hit") is not True


def test_battery_model_invariants_and_capacity_calculations():
    """Verifies Deye 12kW + SunDeposit 16.13 kWh capacity calculations and slider bound clamps."""
    bm = pstryk_engine.BatteryModel(
        capacity_kwh=16.13,
        max_soc=90.0,
        min_soc=20.0,
        shutdown_soc=5.0,
        charge_current_a=100.0,
        discharge_current_a=100.0,
        voltage_v=51.2,
    )
    assert bm.operating_capacity_kwh == 11.29  # (90 - 20)% * 16.13 = 11.291
    assert bm.blackout_reserve_kwh == 2.42    # (20 - 5)% * 16.13 = 2.4195
    assert bm.top_buffer_kwh == 1.61          # (100 - 90)% * 16.13 = 1.613
    assert bm.charge_power_kw == 5.12         # 51.2 * 100 / 1000 = 5.12 kW
    assert bm.discharge_power_kw == 5.12
    assert bm.hours_to_full_charge == 2.21    # 11.29 / 5.12 = 2.205 h

    # Test bound clamping (User constraint: Max SOC >= 50%, Min SOC <= 50% and >= 10%, Shutdown <= 15%)
    bm_clamped = pstryk_engine.BatteryModel(
        capacity_kwh=16.13,
        max_soc=40.0,     # Below 50 -> clamped to 50
        min_soc=60.0,     # Above 50 -> clamped to 50
        shutdown_soc=25.0 # Above 15 -> clamped to 15
    )
    assert bm_clamped.max_soc == 50.0
    assert bm_clamped.min_soc == 50.0
    assert bm_clamped.shutdown_soc == 15.0


def test_ev_charging_window_weekday_and_weekend():
    """Verifies that EV window selects 2h on weekdays and 4h on weekends by default."""
    from datetime import datetime, timezone
    hourly_payload = load_fixture("sample_hourly_today.json")

    # 1. Weekday (e.g. 2026-09-24 was Thursday, weekday=3)
    weekday_dt = datetime(2026, 9, 24, 8, 0, 0, tzinfo=timezone.utc)
    ev_weekday = pstryk_engine.find_ev_best_window(hourly_payload, target_hours="auto", now_dt=weekday_dt)
    assert ev_weekday["duration_hours"] == 2
    assert ev_weekday["average_price"] == 0.305
    assert "15:00 - 17:00" in ev_weekday["display"]
    assert "śr. 0.30 zł/kWh" in ev_weekday["display"]

    # 2. Weekend (e.g. Saturday 2026-09-26, weekday=5)
    weekend_dt = datetime(2026, 9, 26, 8, 0, 0, tzinfo=timezone.utc)
    ev_weekend = pstryk_engine.find_ev_best_window(hourly_payload, target_hours=4, now_dt=weekend_dt)
    assert ev_weekend["duration_hours"] == 4
    assert ev_weekend["min_price"] <= ev_weekend["average_price"] <= ev_weekend["max_price"]
    assert "śr." in ev_weekend["display"]


def test_powerbank_window_continuous_and_disjoint():
    """Verifies Power Bank window calculates lowest hours and handles continuous vs disjoint slots."""
    hourly_payload = load_fixture("sample_hourly_today.json")

    # In sample_hourly_today, the two lowest hours are 13:00 (0.30) and 14:00 (0.31) -> 15:00-17:00 Warsaw time!
    pb_res = pstryk_engine.find_powerbank_best_window(hourly_payload, target_hours=2, allow_disjoint=True)
    assert pb_res["duration_hours"] == 2
    assert pb_res["is_consecutive"] is True
    assert pb_res["display"] == "15:00 - 17:00 (śr. 0.30 zł/kWh)"

    # Test synthetic disjoint frames (e.g. cheapest at 03:00 and 14:00 UTC -> 05:00 and 16:00 Warsaw time)
    synthetic_frames = [
        {"start": "2026-10-06T03:00:00Z", "end": "2026-10-06T04:00:00Z", "metrics": {"pricing": {"price_gross": 0.20}}},
        {"start": "2026-10-06T04:00:00Z", "end": "2026-10-06T05:00:00Z", "metrics": {"pricing": {"price_gross": 0.80}}},
        {"start": "2026-10-06T14:00:00Z", "end": "2026-10-06T15:00:00Z", "metrics": {"pricing": {"price_gross": 0.25}}},
        {"start": "2026-10-06T15:00:00Z", "end": "2026-10-06T16:00:00Z", "metrics": {"pricing": {"price_gross": 0.90}}},
    ]
    disjoint_res = pstryk_engine.find_powerbank_best_window({"frames": synthetic_frames}, target_hours=2, allow_disjoint=True)
    assert disjoint_res["duration_hours"] == 2
    assert disjoint_res["is_consecutive"] is False
    assert disjoint_res["slots"] == ["05:00 - 06:00", "16:00 - 17:00"]
    assert disjoint_res["display"] == "05:00 - 06:00 oraz 16:00 - 17:00 (śr. 0.23 zł/kWh)"


def test_best_sell_window_and_spike_calculation():
    """Verifies peak selling window and 1-hour absolute spike calculation in Warsaw local time."""
    hourly_payload = load_fixture("sample_hourly_today.json")
    sell_res = pstryk_engine.find_best_sell_window(hourly_payload, target_hours=3)

    assert sell_res["duration_hours"] == 3
    assert sell_res["start"] == "2026-09-24T18:00:00Z"
    assert sell_res["end"] == "2026-09-24T21:00:00Z"
    assert sell_res["spike_hour"] == "21:00 - 22:00"
    assert sell_res["spike_price"] == 0.799
    assert "20:00 - 23:00" in sell_res["display"]
    assert "śr. 0.75 zł/kWh" in sell_res["display"]
    assert "pik 21:00: 0.80 zł" in sell_res["display"]


def test_generate_deye_tou_schedule_structure_and_monotonicity():
    """Verifies that generate_deye_tou_schedule produces 6 monotonic slots with valid clamps."""
    pb_win = {"start": "2026-10-06T00:00:00Z", "end": "2026-10-06T02:00:00Z", "is_consecutive": True}
    sell_win = {"start": "2026-10-06T15:00:00Z", "end": "2026-10-06T19:00:00Z"}

    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=85,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_win,
        best_sell_window=sell_win,
        calibration_active=False,
    )

    assert len(slots) == 6
    # Slot 1: 00:00 day anchor -- after the previous peak, before the charge: drained to min_soc
    assert slots[0]["time"] == "00:00" and slots[0]["target_soc"] == 20 and slots[0]["grid_charge"] is False

    # Slot 2: Night charge (02:00)
    assert slots[1]["slot"] == 2
    assert slots[1]["time"] == "02:00"
    assert slots[1]["grid_charge"] is True
    assert slots[1]["target_soc"] == 90

    # Slot 3: hold after the charge until the peak (04:00)
    assert slots[2]["slot"] == 3
    assert slots[2]["time"] == "04:00"
    assert slots[2]["grid_charge"] is False
    assert slots[2]["target_soc"] == 90

    # Slot 5: Sell window (17:00)
    assert slots[4]["slot"] == 5
    assert slots[4]["time"] == "17:00"
    assert slots[4]["grid_charge"] is False
    assert slots[4]["target_soc"] == 20

    # Monotonicity check
    def parse_m(t):
        p = t.split(":")
        return int(p[0]) * 60 + int(p[1])
    minutes = [parse_m(s["time"]) for s in slots]
    assert all(minutes[i] < minutes[i+1] for i in range(len(minutes)-1))


def test_generate_deye_tou_schedule_calibration_mode():
    """Verifies that calibration_active sets Slot 1 target SOC to 100%."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50,
        max_soc=90,
        min_soc=20,
        calibration_active=True,
        best_pb_window={"start": "02:00", "end": "04:00", "duration_hours": 2},
        best_sell_window={"start": "17:00", "end": "21:00"},
    )
    charge = [s for s in slots if s["grid_charge"]][0]
    assert charge["target_soc"] == 100
    assert "Kalibracja" in charge["label"]


def test_generate_deye_tou_schedule_midday_dip():
    """Verifies disjoint midday dip triggers grid_charge=True on Slot 3."""
    pb_win = {
        "start": "2026-10-06T02:00:00Z",
        "end": "2026-10-06T14:00:00Z",
        "is_consecutive": False,
        "slots": ["02:00 - 03:00", "13:00 - 14:00"],
    }
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=80,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_win,
        best_sell_window={"start": "17:00", "end": "21:00"},
    )
    assert slots[2]["time"] == "13:00"
    assert slots[2]["grid_charge"] is True
    assert "Tania Godzina" in slots[2]["label"]


def test_sync_deye_inverter_tou_schedule_integration(tmp_path):
    """Verifies sync_deye_inverter_tou_schedule calls solarman client with 6 slots."""
    cache_dir = str(tmp_path)
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), {
        "_warsaw_date": warsaw_today_iso(),
        "powerbank_best_window": {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T13:00:00Z", "duration_hours": 2, "is_consecutive": True},
        "best_sell_window": {"start": "2026-10-17T15:00:00Z", "end": "2026-10-17T18:00:00Z"},
    })
    with mock.patch("solarman_v5_client.read_inverter_telemetry") as mock_read, \
         mock.patch("solarman_v5_client.sync_tou_schedule") as mock_sync, \
         mock.patch("solarman_v5_client.set_charge_current", return_value={"success": True}) as mock_set:
        mock_read.return_value = {
            "success": True,
            "config": {"max_charge_soc": 90, "min_discharge_soc": 20},
            "telemetry": {"battery_soc": 82},
        }
        mock_sync.return_value = {"success": True, "message": "OK"}

        res = pstryk_engine.sync_deye_inverter_tou_schedule(host="10.20.2.6", port=8899, cache_dir=cache_dir)
        assert res["success"] is True
        assert len(res["slots"]) == 6
        assert res["soc_limits"]["current_soc"] == 82
        mock_sync.assert_called_once()
        mock_set.assert_called_once()


def test_cli_sync_deye_tou_flag():
    """Verifies CLI flag --sync-deye-tou executes cleanly."""
    with mock.patch("pstryk_engine.sync_deye_inverter_tou_schedule", return_value={"success": True}) as mock_sync:
        ret = pstryk_engine.main(["--sync-deye-tou", "--inverter-host", "127.0.0.1"])
        assert ret == 0
        assert mock_sync.call_args.kwargs["host"] == "127.0.0.1"


def test_cli_apply_battery_safety_limits_flag():
    """Verifies CLI flag --apply-battery-safety-limits calls solarman_v5_client."""
    with mock.patch("solarman_v5_client.apply_confirmed_battery_safety_limits") as mock_apply:
        mock_apply.return_value = {"success": True, "min_soc": 15, "shutdown_soc": 5}
        ret = pstryk_engine.main(["--apply-battery-safety-limits", "--min-soc", "15", "--shutdown-soc", "5"])
        assert ret == 0
        mock_apply.assert_called_once_with(
            host=None, port=None, min_soc=15, shutdown_soc=5
        )



def test_resolve_api_key_from_env_installation_specific():
    """Verifies resolve_api_key reads installation-specific variable from .env."""
    with mock.patch.dict(os.environ, {"PSTRYK_API_KEY_DOL": "sk-dol-test-123"}, clear=False):
        key, source = pstryk_engine.resolve_api_key("dol")
        assert key == "sk-dol-test-123"
        assert source == "env"


def test_resolve_api_key_from_env_generic():
    """Verifies resolve_api_key falls back to generic PSTRYK_API_KEY if specific is unset."""
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("PSTRYK_API_KEY")}
    clean_env["PSTRYK_API_KEY"] = "sk-generic-test-456"
    with mock.patch.dict(os.environ, clean_env, clear=True):
        key, source = pstryk_engine.resolve_api_key("gora")
        assert key == "sk-generic-test-456"
        assert source == "env"


def test_resolve_api_key_explicit_arg_precedence():
    """Verifies explicit CLI argument takes precedence over environment variables."""
    with mock.patch.dict(os.environ, {"PSTRYK_API_KEY_DOL": "sk-env-key"}, clear=False):
        key, source = pstryk_engine.resolve_api_key("dol", explicit_key="sk-arg-key")
        assert key == "sk-arg-key"
        assert source == "arg"


def test_cli_execution_using_env_without_key_argument():
    """Verifies CLI executes fetch_consolidated_data cleanly when key is in .env without passing --key."""
    with mock.patch.dict(os.environ, {"PSTRYK_API_KEY_DOL": "sk-env-test"}, clear=False), \
         mock.patch("pstryk_engine.fetch_consolidated_data") as mock_fetch:
        mock_fetch.return_value = {"current_price": 1.23, "start": "12:00", "end": "13:00"}
        ret = pstryk_engine.main(["--json", "--installation", "dol"])
        assert ret == 0
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args[1]["api_key"] == "sk-env-test"


def test_cli_help_string_formatting_validity():
    """Verifies CLI --help formatting does not throw ValueError on Python 3.14 formatter._expand_help."""
    with pytest.raises(SystemExit) as exc_info:
        pstryk_engine.main(["--help"])
def test_format_hhmm_warsaw_timezone():
    """Verifies format_hhmm correctly converts UTC timestamps to Europe/Warsaw in CEST and CET."""
    # Summer CEST (+2h): 11:00 UTC -> 13:00 Warsaw
    assert pstryk_engine.format_hhmm("2026-10-08T11:00:00Z") == "13:00"
    # Winter CET (+1h): 11:00 UTC -> 12:00 Warsaw
    assert pstryk_engine.format_hhmm("2026-01-15T11:00:00Z") == "12:00"
    # Explicit timezone offset (+02:00)
    assert pstryk_engine.format_hhmm("2026-10-08T13:00:00+02:00") == "13:00"
    # Edge cases
    assert pstryk_engine.format_hhmm(None) == "--:--"
    assert pstryk_engine.format_hhmm("") == "--:--"
    assert pstryk_engine.format_hhmm("13:45") == "13:45"


def test_generate_deye_tou_schedule_midday_charge_13_15():
    """Verifies that midday cheapest window (13:00-15:00) configures Slot 3 for grid charging and Slot 5 for peak self-consumption."""
    pb_win = {
        "start": "2026-10-08T11:00:00Z",  # 13:00 Warsaw
        "end": "2026-10-08T13:00:00Z",    # 15:00 Warsaw
        "is_consecutive": True,
    }
    sell_win = {
        "start": "2026-10-08T15:00:00Z",  # 17:00 Warsaw
        "end": "2026-10-08T19:00:00Z",    # 21:00 Warsaw
    }
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=85,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_win,
        best_sell_window=sell_win,
    )

    assert len(slots) == 6
    # Without price frames only the explicit windows are known (no invented morning peak / elevated-price clock):
    # 00:00 after the previous peak and before the charge -> min_soc; 06:00 is the split of that same slot
    assert [(s["time"], s["target_soc"], s["grid_charge"]) for s in slots] == [
        ("00:00", 20, False), ("06:00", 20, False), ("13:00", 90, True),
        ("15:00", 90, False), ("17:00", 20, False), ("21:00", 20, False),
    ]
    assert "Najtańsze Ładowanie" in slots[2]["label"]
    # Slot 4: after the charge, hold Max SOC until the peak
    assert "Hold do Szczytu" in slots[3]["label"]
    # Slot 5: 100% peak coverage down to 20%
    assert "Szczyt" in slots[4]["label"] and slots[4]["role"] == "peak"


def test_today_and_tomorrow_frames_partitioning(tmp_path):
    """
    Verifies that fetch_consolidated_data cleanly isolates Today (00:00-24:00) from
    Tomorrow (00:00-24:00) into dedicated dispatch windows and sets has_tomorrow_pricing=True.
    """
    cache_dir = str(tmp_path)
    now_warsaw = datetime.now(pstryk_engine.WARSAW_TZ)
    today_date = now_warsaw.date()
    tomorrow_date = (now_warsaw + timedelta(days=1)).date()

    # Generate 24 frames for today and 24 frames for tomorrow
    frames = []
    for d, base_price in [(today_date, 0.80), (tomorrow_date, 0.40)]:
        for hour in range(24):
            dt = datetime(d.year, d.month, d.day, hour, 0, tzinfo=pstryk_engine.WARSAW_TZ)
            iso_start = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            iso_end = (dt + timedelta(hours=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            price = base_price + (0.10 if 17 <= hour <= 20 else (-0.20 if 13 <= hour <= 14 else 0.0))
            frames.append({
                "start": iso_start,
                "end": iso_end,
                "metrics": {
                    "pricing": {
                        "full_price": price,
                        "price_gross": price,
                        "price_net": price * 0.8,
                        "price_prosumer_net": price * 0.8,
                        "price_prosumer_gross": price,
                    }
                }
            })

    today_payload = {"frames": [f for f in frames if datetime.fromisoformat(f["start"].replace("Z", "+00:00")).astimezone(pstryk_engine.WARSAW_TZ).date() == today_date]}
    forward_payload = {"frames": frames}

    def mock_fetch(url, params, api_key, timeout=10):
        if params.get("temporal") == "latest":
            return {"frames": [frames[0]]}
        if params.get("temporal") == "today":
            return today_payload
        if params.get("resolution") == "month":
            return {"frames": []}
        if params.get("resolution") == "day":
            return {"frames": []}
        return forward_payload

    with mock.patch("pstryk_engine.fetch_api", side_effect=mock_fetch):
        res = pstryk_engine.fetch_consolidated_data(
            api_key="sk-TEST",
            installation="dol",
            use_cache=False,
            cache_dir=cache_dir,
        )

    assert res["has_tomorrow_pricing"] is True
    assert "ev_best_window_today" in res
    assert "powerbank_best_window_today" in res
    assert "best_sell_window_today" in res
    assert "deye_tou_schedule_today" in res
    assert len(res["deye_tou_schedule_today"]) == 6

    assert "ev_best_window_tomorrow" in res
    assert "powerbank_best_window_tomorrow" in res
    assert "best_sell_window_tomorrow" in res
    assert "deye_tou_schedule_tomorrow" in res
    assert len(res["deye_tou_schedule_tomorrow"]) == 6
    assert res["powerbank_best_window_tomorrow"]["display"] != "Oczekiwanie na publikację (ok. 12:00)"


def test_tomorrow_pending_publication_before_12(tmp_path):
    """
    Verifies that when tomorrow's frames are not yet published, has_tomorrow_pricing is False
    and display strings show 'Oczekiwanie na publikację (ok. 12:00)'.
    """
    cache_dir = str(tmp_path)
    now_warsaw = datetime.now(pstryk_engine.WARSAW_TZ)
    today_date = now_warsaw.date()

    # Only 24 frames for today
    frames = []
    for hour in range(24):
        dt = datetime(today_date.year, today_date.month, today_date.day, hour, 0, tzinfo=pstryk_engine.WARSAW_TZ)
        iso_start = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        iso_end = (dt + timedelta(hours=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        frames.append({
            "start": iso_start,
            "end": iso_end,
            "metrics": {
                "pricing": {
                    "full_price": 0.75,
                    "price_gross": 0.75,
                    "price_net": 0.60,
                    "price_prosumer_net": 0.60,
                    "price_prosumer_gross": 0.75,
                }
            }
        })

    today_payload = {"frames": frames}

    def mock_fetch(url, params, api_key, timeout=10):
        if params.get("temporal") == "latest":
            return {"frames": [frames[0]]}
        return today_payload

    with mock.patch("pstryk_engine.fetch_api", side_effect=mock_fetch):
        res = pstryk_engine.fetch_consolidated_data(
            api_key="sk-TEST",
            installation="dol",
            use_cache=False,
            cache_dir=cache_dir,
        )

    assert res["has_tomorrow_pricing"] is False
    pending_msg = "Oczekiwanie na publikację (ok. 12:00)"
    assert res["powerbank_best_window_tomorrow"]["display"] == pending_msg
    assert res["best_sell_window_tomorrow"]["display"] == pending_msg
    assert res["ev_best_window_tomorrow"]["display"] == pending_msg
    assert res["deye_tou_schedule_tomorrow"] == []


def test_sync_deye_inverter_tou_schedule_for_tomorrow(tmp_path):
    """
    Verifies that sync_deye_inverter_tou_schedule(..., for_tomorrow=True) uses tomorrow's windows
    when tomorrow pricing is available.
    """
    cache_dir = str(tmp_path)
    cached_payload = {
        "_warsaw_date": warsaw_today_iso(),
        "has_tomorrow_pricing": True,
        "powerbank_best_window": {"start": "2026-10-08T11:00:00Z", "end": "2026-10-08T13:00:00Z", "is_consecutive": True},
        "best_sell_window": {"start": "2026-10-08T15:00:00Z", "end": "2026-10-08T19:00:00Z"},
        "powerbank_best_window_tomorrow": {"start": "2026-10-09T11:00:00Z", "end": "2026-10-09T13:00:00Z", "is_consecutive": True},
        "best_sell_window_tomorrow": {"start": "2026-10-09T16:00:00Z", "end": "2026-10-09T20:00:00Z"},
    }
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), cached_payload)

    mock_client = mock.MagicMock()
    mock_client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {}}
    mock_client.sync_tou_schedule.return_value = {"success": True}
    with mock.patch("pstryk_engine.solarman_v5_client", mock_client):
        result = pstryk_engine.sync_deye_inverter_tou_schedule(
            cache_dir=cache_dir,
            for_tomorrow=True,
        )
    assert len(result["slots"]) == 6


def test_powerbank_weekend_auto_duration_and_weekday_fixed():
    """
    Rewritten (no weekday/weekend branching): the auto window comes from energy need and the price valley only.
    The same price curve on a Friday and a Saturday must give the identical window, never shorter than the
    energy-need minimum and never leaving the valley that contains it.
    """
    def curve(h):
        return 0.50 if 11 <= h <= 15 else 1.20

    results = {}
    for label, day in (("fri", datetime(2026, 10, 16).date()), ("sat", datetime(2026, 10, 17).date())):
        frames = warsaw_day_frames(day, curve)
        res = pstryk_engine.find_powerbank_best_window({"frames": frames}, target_hours="auto", **ENERGY_KW)
        results[label] = (res, frames)
    fri, fri_frames = results["fri"]
    sat, _ = results["sat"]
    assert _win_minutes(fri) == _win_minutes(sat)
    assert fri["duration_hours"] == sat["duration_hours"] >= _min_hours()
    assert fri["is_consecutive"] is True
    valley = _valley_containing(fri_frames, _win_minutes(fri)[0])
    assert valley["start_m"] <= _win_minutes(fri)[0] and _win_minutes(fri)[1] <= valley["end_m"]


def test_proportional_charging_power_scaling_and_slot5_protection():
    """
    Verifies that TOU power_w across all slots strictly stays at base discharge power (7170W for 140A * 51.2V),
    while the charging slot dynamically modulates target_charge_current_a (Register 108) based on window duration
    (ceiling*min_h/T, min_h from energy need), and Slot 5 (Peak Evening Discharge) strictly retains full base power.
    """
    pb_4h = {"start": "2026-10-17T09:00:00Z", "end": "2026-10-17T13:00:00Z", "duration_hours": 4, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T15:00:00Z", "end": "2026-10-17T18:00:00Z"}

    # Case A: Half-full battery (50% to 90%, 4h window):
    # TOU power_w is strictly base discharge power (7170W for 140A * 51.2V)
    # Register 108 target charging current is scaled for 4h: 100A * (2 / 4) = 50A
    slots_50 = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_4h,
        best_sell_window=sell_win,
        calibration_active=False,
        max_charge_current=100.0,
        max_discharge_current=140.0,
        battery_capacity_kwh=ENERGY_KW['capacity_kwh'], nominal_voltage=ENERGY_KW['voltage'], loss_buffer_pct=ENERGY_KW['loss_pct'],
    )
    charge_slot_50 = [s for s in slots_50 if s["grid_charge"] is True][0]
    assert charge_slot_50["power_w"] == 7170
    assert charge_slot_50["target_charge_current_a"] == _expected_current(_min_hours(50), 4)
    assert charge_slot_50["base_charge_current_a"] == 100

    # Slot 5 (Peak Discharge) strictly full base power (7170W)
    assert slots_50[4]["slot"] == 5
    assert slots_50[4]["power_w"] == 7170
    assert slots_50[4]["grid_charge"] is False
    assert slots_50[4]["target_soc"] == 20

    # Case B: Empty battery (20% to 90%, 4h window):
    slots_20 = pstryk_engine.generate_deye_tou_schedule(
        current_soc=20,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_4h,
        best_sell_window=sell_win,
        calibration_active=False,
        max_charge_current=100.0,
        max_discharge_current=140.0,
        battery_capacity_kwh=ENERGY_KW['capacity_kwh'], nominal_voltage=ENERGY_KW['voltage'], loss_buffer_pct=ENERGY_KW['loss_pct'],
    )
    charge_slot_20 = [s for s in slots_20 if s["grid_charge"] is True][0]
    assert charge_slot_20["power_w"] == 7170
    assert charge_slot_20["target_charge_current_a"] == _expected_current(_min_hours(20), 4)

    # Case C: 2h window -> full 100A charging speed:
    pb_2h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T13:00:00Z", "duration_hours": 2, "is_consecutive": True}
    slots_2h = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_2h,
        best_sell_window=sell_win,
        calibration_active=False,
        max_charge_current=100.0,
        max_discharge_current=140.0,
        battery_capacity_kwh=ENERGY_KW['capacity_kwh'], nominal_voltage=ENERGY_KW['voltage'], loss_buffer_pct=ENERGY_KW['loss_pct'],
    )
    charge_slot_2h = [s for s in slots_2h if s["grid_charge"] is True][0]
    assert charge_slot_2h["power_w"] == 7170
    assert charge_slot_2h["target_charge_current_a"] == _expected_current(_min_hours(50), 2)


def test_dynamic_inverter_max_charge_current_scaling():
    """
    Verifies that base discharge power dynamically adapts when max_discharge_current is 140A (7170W)
    or 200A (10240W) at nominal 51.2V, while target_charge_current_a adapts to max_charge_current.
    """
    pb_2h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T13:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T15:00:00Z", "end": "2026-10-17T18:00:00Z"}

    # 140A discharge @ 51.2V -> 7170W base power across all slots.
    slots_140 = pstryk_engine.generate_deye_tou_schedule(
        current_soc=20,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_2h,
        best_sell_window=sell_win,
        max_charge_current=100.0,
        max_discharge_current=140.0,
    )
    charge_140 = [s for s in slots_140 if s["grid_charge"] is True][0]
    assert charge_140["power_w"] == 7170
    assert charge_140["target_charge_current_a"] == 100
    assert slots_140[4]["power_w"] == 7170

    # 200A discharge @ 51.2V -> 10240W base power.
    slots_200 = pstryk_engine.generate_deye_tou_schedule(
        current_soc=20,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_2h,
        best_sell_window=sell_win,
        max_charge_current=100.0,
        max_discharge_current=200.0,
    )
    charge_200 = [s for s in slots_200 if s["grid_charge"] is True][0]
    assert charge_200["power_w"] == 10240
    assert slots_200[4]["power_w"] == 10240


def test_charge_stretch_excludes_hours_above_valley_mean():
    """
    Stretch rule (pure data, no tolerance): an adjacent valley hour joins the window only if its gross price is
    <= that valley's mean gross price. Valley 01-06 = 0.78/0.80/0.82/0.84/edge; the edge hour (above the valley
    mean in both runs) is never added, the hours at or below the mean are, and the window stays inside the valley.
    """
    for edge_price in (1.28, 1.00):
        prices = [1.40] * 24
        for h, p in zip(range(1, 6), (0.78, 0.80, 0.82, 0.84, edge_price)):
            prices[h] = p
        frames = warsaw_day_frames(datetime(2026, 10, 17).date(), lambda h: prices[h])
        res = pstryk_engine.find_powerbank_best_window({"frames": frames}, target_hours="auto", **ENERGY_KW)
        valley = [s for s in _segments(pstryk_engine.analyse_day_price_profile(frames)) if s["kind"] == "valley"][0]
        valley_hours = range(valley["start_m"] // 60, valley["end_m"] // 60)
        assert prices[5] > valley["avg"]
        expected = [h for h in valley_hours if prices[h] <= valley["avg"]]
        start, end = _win_minutes(res)
        assert list(range(start // 60, end // 60)) == expected


def test_save_last_tou_sync_result(tmp_path):
    """Verifies that save_last_tou_sync_result persists JSON data properly."""
    test_file = tmp_path / ".deye_tou_last_sync.json"
    with mock.patch("pstryk_engine.LAST_TOU_SYNC_PATHS", [str(test_file)]):
        pstryk_engine.save_last_tou_sync_result({"success": True, "for_tomorrow": False, "slots_count": 6})
    assert test_file.exists()
    with open(test_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["success"] is True
    assert data["slots_count"] == 6


def test_soc_delta_dynamic_charging_power_matrix():
    """
    Verifies that TOU power_w across all slots stays at base discharge power (7170W for 140A * 51.2V),
    while target_charge_current_a scales dynamically based on charging window duration (Register 108 modulation):
    - 2h window -> 100A (100% speed)
    - 3h window -> 67A (100 * 2 / 3)
    - 4h window -> 50A (100 * 2 / 4, 50% speed)
    """
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}
    win_2h = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    win_3h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T14:00:00Z", "duration_hours": 3, "is_consecutive": True}
    win_4h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 4, "is_consecutive": True}

    def get_schedule(curr_soc, win):
        return pstryk_engine.generate_deye_tou_schedule(
            current_soc=curr_soc,
            max_soc=90,
            min_soc=20,
            best_pb_window=win,
            best_sell_window=sell_win,
            calibration_active=False,
            max_charge_current=100.0,
            max_discharge_current=140.0,
        )

    # Case 1: Empty battery (20%), 2h window -> 7170W discharge, 100A charge current
    s_2h = get_schedule(20, win_2h)
    charge_2h = [slot for slot in s_2h if slot["grid_charge"] is True][0]
    assert charge_2h["power_w"] == 7170
    assert charge_2h["target_charge_current_a"] == 100

    # Case 2: 3h window -> 7170W discharge, 67A charge current (100 * 2 / 3)
    s_3h = get_schedule(50, win_3h)
    charge_3h = [slot for slot in s_3h if slot["grid_charge"] is True][0]
    assert charge_3h["power_w"] == 7170
    assert charge_3h["target_charge_current_a"] == 67

    # Case 3: 4h window -> 7170W discharge, 50A charge current (100 * 2 / 4)
    s_4h = get_schedule(50, win_4h)
    charge_4h = [slot for slot in s_4h if slot["grid_charge"] is True][0]
    assert charge_4h["power_w"] == 7170
    assert charge_4h["target_charge_current_a"] == 50


def test_peak_slots_follow_min_soc():
    """
    The slots around the only peak follow min_soc dynamically (10/20/25%): after the peak and before the next
    charge the battery drains to min_soc and the peak itself discharges to min_soc.
    """
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    for min_soc in (10, 20, 25):
        slots = pstryk_engine.generate_deye_tou_schedule(
            min_soc=min_soc, max_soc=90, best_pb_window=pb_win, best_sell_window=sell_win
        )
        assert_valid_schedule(slots)
        assert slots[0]["target_soc"] == min_soc       # 00:00: after the peak, before the charge
        assert [s["target_soc"] for s in slots if s["role"] == "peak"] == [min_soc]
        assert slots[5]["target_soc"] == min_soc       # post-peak standby


def test_uninstalled_battery_zero_soc_safeguard():
    """
    Verifies that when battery is not installed or reporting 0% SOC,
    current_soc <= 0 safely defaults to safe_min_soc without raising errors.
    """
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    slots_zero = pstryk_engine.generate_deye_tou_schedule(
        current_soc=0,
        max_soc=90,
        min_soc=20,
        best_pb_window=pb_win,
        best_sell_window=sell_win,
    )
    charge_slot = [s for s in slots_zero if s["grid_charge"] is True][0]
    # Retains base discharge power (7170W for 140A * 51.2V)
    assert charge_slot["power_w"] in (5000, 5120, 7170)


def test_are_tou_slots_identical_matching_and_tolerance():
    """
    Verifies the idempotent diff check logic:
    - Exactly identical slots -> True
    - Slots within +-50W tolerance -> True
    - Slots differing by >50W -> False
    - Slots differing by target_soc, time, or grid_charge -> False
    """
    base_slots = [
        {"slot": 1, "time": "00:00", "power_w": 5000, "target_soc": 30, "grid_charge": False},
        {"slot": 2, "time": "06:00", "power_w": 5000, "target_soc": 20, "grid_charge": False},
        {"slot": 3, "time": "13:00", "power_w": 3390, "target_soc": 90, "grid_charge": True},
        {"slot": 4, "time": "15:00", "power_w": 5000, "target_soc": 70, "grid_charge": False},
        {"slot": 5, "time": "17:00", "power_w": 5000, "target_soc": 20, "grid_charge": False},
        {"slot": 6, "time": "21:00", "power_w": 5000, "target_soc": 30, "grid_charge": False},
    ]

    # Identical copy
    assert pstryk_engine.are_tou_slots_identical(base_slots, [dict(s) for s in base_slots]) is True

    # Within 30W tolerance (e.g. 3390 vs 3420)
    tol_slots = [dict(s) for s in base_slots]
    tol_slots[2]["power_w"] = 3420
    assert pstryk_engine.are_tou_slots_identical(base_slots, tol_slots) is True

    # Beyond tolerance (e.g. 3390 vs 3500, diff = 110W)
    diff_pwr = [dict(s) for s in base_slots]
    diff_pwr[2]["power_w"] = 3500
    assert pstryk_engine.are_tou_slots_identical(base_slots, diff_pwr) is False

    # Different time
    diff_time = [dict(s) for s in base_slots]
    diff_time[2]["time"] = "12:00"
    assert pstryk_engine.are_tou_slots_identical(base_slots, diff_time) is False

    # Different SOC
    diff_soc = [dict(s) for s in base_slots]
    diff_soc[0]["target_soc"] = 20
    assert pstryk_engine.are_tou_slots_identical(base_slots, diff_soc) is False

    # Different grid charge flag
    diff_grid = [dict(s) for s in base_slots]
    diff_grid[2]["grid_charge"] = False
    assert pstryk_engine.are_tou_slots_identical(base_slots, diff_grid) is False


def test_sync_deye_inverter_tou_schedule_idempotent_skip_and_force(tmp_path):
    """
    Verifies that sync_deye_inverter_tou_schedule skips write if schedule matches active schedule,
    and forces write when force=True.
    """
    cache_dir = str(tmp_path)
    # Mock cached data
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}
    fake_cache = {
        "_warsaw_date": warsaw_today_iso(),
        "current": {}, "today": {}, "all_prices": [],
        "powerbank_best_window": pb_win,
        "best_sell_window": sell_win,
    }
    c_file = pstryk_engine.get_cache_file_path("dol", cache_dir)
    pstryk_engine.write_cache(c_file, fake_cache)

    expected_slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50, max_soc=90, min_soc=20,
        best_pb_window=pb_win, best_sell_window=sell_win,
        max_charge_current=100.0,
    )

    mock_telem = {
        "success": True,
        "config": {"max_charge_soc": 90, "min_discharge_soc": 20, "max_charge_current": 100},
        "telemetry": {"battery_soc": 50, "max_charge_current": 100},
        "active_tou": {"enabled": True, "slots": expected_slots},
    }

    mock_client = mock.MagicMock()
    mock_client.read_inverter_telemetry.return_value = mock_telem
    mock_client.sync_tou_schedule.return_value = {"success": True, "registers_written": 26}
    mock_client.get_configured_inverter_ip.return_value = "10.20.2.6"
    mock_client.get_configured_inverter_port.return_value = 8899
    mock_client.set_charge_current.return_value = {"success": True}

    with mock.patch("pstryk_engine.solarman_v5_client", mock_client):
        # 1. Without force -> diff check matches -> write skipped (unchanged: True)
        res_skipped = pstryk_engine.sync_deye_inverter_tou_schedule(
            cache_dir=cache_dir,
            force=False,
        )
        assert res_skipped["success"] is True
        assert res_skipped["unchanged"] is True
        assert "pominięto zapis" in res_skipped["message"]
        mock_client.sync_tou_schedule.assert_not_called()

        # 2. With force=True -> diff check bypassed -> sync_tou_schedule called!
        res_forced = pstryk_engine.sync_deye_inverter_tou_schedule(
            cache_dir=cache_dir,
            force=True,
        )
        assert res_forced["success"] is True
        assert res_forced["unchanged"] is False
        mock_client.sync_tou_schedule.assert_called_once()


def test_fetch_consolidated_custom_soc_and_battery_model(tmp_path, monkeypatch):
    """
    Asserts that fetch_consolidated_data correctly adopts user-supplied max_soc,
    min_soc, and current_soc into both BatteryModel and the generated TOU schedules.
    """
    cache_dir = str(tmp_path / "cache_soc")
    os.makedirs(cache_dir, exist_ok=True)

    freeze_now(monkeypatch, datetime(2026, 10, 16, 0, 30, tzinfo=WARSAW))
    # household load the main charge covers (no telemetry here -> the rated-discharge fallback would add a top-up)
    monkeypatch.setattr(pstryk_engine, "household_load_kw", lambda *a, **k: 0.5)
    day = datetime(2026, 10, 16).date()
    price = lambda h: 0.3 if 13 <= h < 15 else (1.2 if 18 <= h < 21 else 0.7)
    dummy_payload = {"frames": warsaw_day_frames(day, price) + warsaw_day_frames(day + timedelta(days=1), price)}

    with mock.patch("pstryk_engine.fetch_api", return_value=dummy_payload):
        consolidated = pstryk_engine.fetch_consolidated_data(
            api_key="sk-CUSTOM-SOC",
            installation="dol",
            use_cache=False,
            cache_dir=cache_dir,
            max_soc=80,
            min_soc=15,
            current_soc=40,
        )

        # Verify BatteryModel reflects custom limits
        bm = consolidated.get("battery_model", {})
        assert bm.get("max_soc") == 80.0
        assert bm.get("min_soc") == 15.0
        assert bm.get("operating_capacity_kwh") == round((80.0 - 15.0) / 100.0 * 16.13, 2)

        # Verify TOU schedule reflects safe_max_soc = 80 and min_soc = 15 (no clock-invented morning reserve):
        # one valley 00-18 (0.3/0.7 < day mean), peak 18-21; the 0.7 hours sit above the valley mean, so the
        # charge window stays on the cheapest hours (price 0.3) and the battery holds max_soc until the peak
        tou_slots = consolidated.get("deye_tou_schedule_today", [])
        assert len(tou_slots) == 6
        cheapest = [f"{h:02d}:00" for h in range(24) if price(h) == min(price(x) for x in range(24))]
        assert [s["time"] for s in tou_slots if s["grid_charge"]] == [cheapest[0]]
        charge_i = next(i for i, s in enumerate(tou_slots) if s["grid_charge"])
        assert tou_slots[charge_i + 1]["time"] == f"{int(cheapest[-1][:2]) + 1:02d}:00"
        # the hold after the charge drains only to the 3 h peak's need: 15 + 3 h * 0.5 kW / capacity (25 %)
        assert [s["target_soc"] for s in tou_slots] == [15, 15, 80, 25, 15, 15]
        assert [s["time"] for s in tou_slots if s["role"] == "peak"] == ["18:00"]


def test_dynamic_capacity_scaling_custom_kwh():
    """
    Verifies that battery_capacity_kwh dynamically scales energy needed and charging power.
    Compares 16.13 kWh with 32.26 kWh (e.g. 2 parallel SunDeposit packs).
    """
    pb_4h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 4, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    # 1. Standard pack:
    # TOU power_w is base discharge power (7170W for 140A * 51.2V)
    # Register 108: 40% of 16.13 kWh * 1.1 = 7.1 kWh -> min_h = 2 at 5.12 kW -> 100A * 2 / 4 = 50A
    s_standard = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50, max_soc=90, min_soc=20,
        best_pb_window=pb_4h, best_sell_window=sell_win,
        battery_capacity_kwh=16.13,
        loss_buffer_pct=10.0,
        max_charge_current=100.0,
        max_discharge_current=140.0,
    )
    charge_standard = [s for s in s_standard if s["grid_charge"] is True][0]
    assert charge_standard["power_w"] == 7170
    assert charge_standard["target_charge_current_a"] == 50

    # 2. Doubled pack:
    s_doubled = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50, max_soc=90, min_soc=20,
        best_pb_window=pb_4h, best_sell_window=sell_win,
        battery_capacity_kwh=32.26,
        loss_buffer_pct=10.0,
        max_charge_current=100.0,
        max_discharge_current=140.0,
    )
    charge_doubled = [s for s in s_doubled if s["grid_charge"] is True][0]
    assert charge_doubled["power_w"] == 7170
    # 40% of 32.26 kWh * 1.1 = 14.2 kWh -> min_h = 3 at 5.12 kW -> 100 A * 3 / 4 = 75 A
    assert charge_doubled["target_charge_current_a"] == 75

    # 3. 2h window with 140A requested charge current (clamped to BATTERY_CHARGE_CURRENT_MAX_CEILING_A = 100A):
    pb_2h = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    s_doubled_2h = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50, max_soc=90, min_soc=20,
        best_pb_window=pb_2h, best_sell_window=sell_win,
        battery_capacity_kwh=32.26,
        loss_buffer_pct=10.0,
        max_charge_current=140.0,
        max_discharge_current=140.0,
    )
    charge_doubled_2h = [s for s in s_doubled_2h if s["grid_charge"] is True][0]
    assert charge_doubled_2h["power_w"] == 7170
    assert charge_doubled_2h["target_charge_current_a"] == 100


def test_loss_buffer_percentage_variations():
    """
    Verifies that TOU discharge power remains at base (7170W) while 3h window scales
    target_charge_current_a to 67A (100 * 2 / 3).
    """
    pb_3h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T14:00:00Z", "duration_hours": 3, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    s_3h = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50, max_soc=90, min_soc=20,
        best_pb_window=pb_3h, best_sell_window=sell_win,
        loss_buffer_pct=10.0,
        max_charge_current=100.0,
        max_discharge_current=140.0,
    )
    charge_3h = [s for s in s_3h if s["grid_charge"] is True][0]
    assert charge_3h["power_w"] == 7170
    assert charge_3h["target_charge_current_a"] == 67


def test_custom_nominal_voltage_and_power_scaling():
    """
    Verifies that nominal_voltage scales base discharge power:
    P_base = min(12000, max(1000, round(I_discharge * V_nom, -1))).
    """
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    # 1. 48.0V nominal voltage at 140A discharge -> 6720W
    s_48 = pstryk_engine.generate_deye_tou_schedule(
        current_soc=20, max_soc=90, min_soc=20,
        best_pb_window=pb_win, best_sell_window=sell_win,
        nominal_voltage=48.0,
        max_discharge_current=140.0,
    )
    assert [s for s in s_48 if s["grid_charge"] is True][0]["power_w"] == 6720
    assert s_48[4]["power_w"] == 6720  # Slot 5

    # 2. 53.0V nominal voltage at 140A discharge -> 7420W
    s_53 = pstryk_engine.generate_deye_tou_schedule(
        current_soc=20, max_soc=90, min_soc=20,
        best_pb_window=pb_win, best_sell_window=sell_win,
        nominal_voltage=53.0,
        max_discharge_current=140.0,
    )
    assert [s for s in s_53 if s["grid_charge"] is True][0]["power_w"] == 7420
    assert s_53[4]["power_w"] == 7420  # Slot 5


def test_resolve_battery_hardware_parameters_4tuple():
    """
    Verifies resolve_battery_hardware_parameters returns a 4-tuple:
    (capacity_kwh, nominal_voltage, max_charge_current, max_discharge_current).
    """
    cap, volts, charge_a, discharge_a = pstryk_engine.resolve_battery_hardware_parameters(
        battery_capacity_kwh=None,
        nominal_voltage=None,
        max_charge_current=None,
        max_discharge_current=None,
    )
    assert cap == energy_defaults.FALLBACK_BATTERY_CAPACITY_KWH
    assert volts == energy_defaults.FALLBACK_BATTERY_NOMINAL_VOLTAGE
    assert charge_a == energy_defaults.FALLBACK_BATTERY_MAX_CHARGE_CURRENT
    assert discharge_a == energy_defaults.FALLBACK_BATTERY_MAX_DISCHARGE_CURRENT


def test_resolve_battery_dispatch_parameters_priority():
    """
    Verifies that resolve_battery_dispatch_parameters:
    1. Honors explicit arguments.
    2. Falls back to inverter config/telemetry cache.
    3. Falls back to energy_defaults.
    4. Clamps within safe operating ranges.
    """
    # 1. Explicit arguments
    res = pstryk_engine.resolve_battery_dispatch_parameters(
        max_soc=85,
        min_soc=18,
        shutdown_soc=8,
        current_soc=55,
        loss_buffer_pct=12.0,
    )
    assert res == (85, 18, 8, 55, 12.0)

    # 2. Clamping bounds
    res_clamped = pstryk_engine.resolve_battery_dispatch_parameters(
        max_soc=120,
        min_soc=5,
        shutdown_soc=25,
        current_soc=150,
    )
    assert res_clamped[0] == 100  # max clamped to 100
    assert res_clamped[1] == 10   # min clamped to 10
    assert res_clamped[2] == 15   # shutdown clamped to 15
    assert res_clamped[3] == 100  # current clamped to max_soc

    # 3. Fallback to inv_cfg
    mock_cfg = {"max_charge_soc": 88, "min_discharge_soc": 16, "shutdown_soc": 6}
    mock_tlm = {"battery_soc": 72}
    res_cfg = pstryk_engine.resolve_battery_dispatch_parameters(
        inv_cfg=mock_cfg,
        inv_tlm=mock_tlm,
    )
    assert res_cfg[0] == 88
    assert res_cfg[1] == 16
    assert res_cfg[2] == 6
    assert res_cfg[3] == 72
    assert res_cfg[4] == energy_defaults.DEFAULT_LOSS_BUFFER_PCT


def test_generate_deye_tou_schedule_variant_b_night_branch():
    """
    Verifies Variant B calculation for Night charging branch (01:00-03:00):
    - min_soc = 16%
    - max_soc = 90%
    """
    pb_win = {
        "start": "2026-10-18T23:00:00Z",  # 01:00 Warsaw
        "end": "2026-10-19T01:00:00Z",    # 03:00 Warsaw
        "is_consecutive": True,
    }
    sell_win = {
        "start": "2026-10-19T15:00:00Z",  # 17:00 Warsaw
        "end": "2026-10-19T18:00:00Z",    # 20:00 Warsaw
    }
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50,
        max_soc=90,
        min_soc=16,
        best_pb_window=pb_win,
        best_sell_window=sell_win,
    )

    assert len(slots) == 6
    # 00:00 day anchor: after the previous day's peak, before the 01:00 charge -> safe_min_soc (16%)
    # 01:00 charge to 90%; 03:00 (and its 10:00 split) hold 90% until the 17:00 peak;
    # 17:00 peak discharges to safe_min_soc; 20:00 drains to safe_min_soc before the next charge
    assert [(s["time"], s["target_soc"], s["grid_charge"]) for s in slots] == [
        ("00:00", 16, False), ("01:00", 90, True), ("03:00", 90, False),
        ("10:00", 90, False), ("17:00", 16, False), ("20:00", 16, False),
    ]
    assert slots[4]["role"] == "peak" and "Szczyt" in slots[4]["label"]
    assert "Hold do Szczytu" in slots[2]["label"]


def test_fetch_consolidated_cache_hit_reevaluates_tou(tmp_path):
    """
    Verifies that cache hit in fetch_consolidated_data immediately re-evaluates
    TOU schedule and BatteryModel when dispatch sliders change, without waiting for TTL.
    """
    cache_dir = str(tmp_path / "cache_reeval")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, "pstryk_cache_dol.json")

    mock_cached = {
        "_warsaw_date": warsaw_today_iso(),
        "current": {"price_gross": 0.85},
        "today": {"hourly": []},
        "all_prices": [{"price_gross": 0.85}],
        "powerbank_best_window": {
            "start": "2026-10-18T23:00:00Z",  # 01:00 Warsaw
            "end": "2026-10-19T01:00:00Z",    # 03:00 Warsaw
            "is_consecutive": True,
        },
        "best_sell_window": {
            "start": "2026-10-19T15:00:00Z",  # 17:00 Warsaw
            "end": "2026-10-19T18:00:00Z",    # 20:00 Warsaw
        },
        "deye_tou_schedule_today": [],
        "_timestamp": time.time(),
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(mock_cached, f)

    # Initial call with min_soc = 16
    res1 = pstryk_engine.fetch_consolidated_data(
        api_key="test_key",
        installation="dol",
        cache_dir=cache_dir,
        min_soc=16,
        max_soc=90,
    )
    assert res1.get("_cache_hit") is True
    sched1 = res1.get("deye_tou_schedule_today", [])
    assert len(sched1) == 6
    assert sched1[4]["target_soc"] == 16  # Peak discharge slot at min_soc = 16
    assert res1.get("battery_model", {}).get("min_soc") == 16.0

    # Subsequent call with changed slider min_soc = 25
    res2 = pstryk_engine.fetch_consolidated_data(
        api_key="test_key",
        installation="dol",
        cache_dir=cache_dir,
        min_soc=25,
        max_soc=90,
    )
    assert res2.get("_cache_hit") is True
    sched2 = res2.get("deye_tou_schedule_today", [])
    assert len(sched2) == 6
    assert sched2[4]["target_soc"] == 25  # Peak discharge slot updated immediately to min_soc = 25
    assert res2.get("battery_model", {}).get("min_soc") == 25.0


def test_battery_charge_current_ceiling_clamp_and_discharge_readonly():
    """
    Verifies that:
    1. Register 108 (Max Charge Current) can never exceed BATTERY_CHARGE_CURRENT_MAX_CEILING_A (100A).
    2. Register 109 (Max Discharge Current) is strictly read-only and never modified.
    """
    import energy_defaults
    import solarman_v5_client
    import deye_cloud_client

    assert energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A == 100.0

    # 1. Modulation clamp
    with mock.patch("solarman_v5_client.set_charge_current", return_value={"success": True}):
        mod_res = pstryk_engine.check_and_apply_charge_current_modulation(
            slots=[],
            base_charge_current=150.0,
            next_cycle_target_a=140,
        )
    assert mod_res["desired_a"] <= 100

    # 2. Solarman client clamp
    with mock.patch("solarman_v5_client.write_single_register") as mock_write, \
         mock.patch("solarman_v5_client.load_deye_cache", return_value={"config": {"max_charge_current": 50}}):
        mock_write.return_value = {"success": True, "message": "OK"}
        res = solarman_v5_client.set_charge_current(host="10.20.2.6", current_a=150)
        assert res["current_a"] == 100
        # Verify written register is 108 (REG_MAX_CHARGE_CURRENT), never 109
        mock_write.assert_called_once()
        assert mock_write.call_args[0][2] == 108
        assert mock_write.call_args[0][3] == 100

    # 3. Deye cloud client clamp
    with mock.patch("deye_cloud_client.call_deye_api") as mock_cloud, \
         mock.patch("solarman_v5_client.load_deye_cache", return_value={"config": {"max_charge_current": 50}}):
        mock_cloud.return_value = (True, {"code": "0", "success": True}, "OK")
        res_cloud = deye_cloud_client.set_charge_current_cloud(
            current_a=160,
            cfg={"app_id": "test", "app_secret": "test", "email": "test", "inverter_sn": "2603160727"}
        )
        assert res_cloud["current_a"] == 100
        mock_cloud.assert_called_once()
        payload = mock_cloud.call_args[1]["data"]
        assert payload["value"] == 100



# ---------------------------------------------------------------------------
# Regression tests: Reg 108 ratchet (A3), night modulation (B4), slot ordering (B1/B6),
# Warsaw day/weekday (B3/B7), price coverage (B5), target-hours CLI (A4), cloud fallback (B8)
# ---------------------------------------------------------------------------

def win(h1, h2, day=(2026, 10, 17)):
    """Window dict for Warsaw hours h1..h2 (h2 may be 24) as UTC ISO strings."""
    base = datetime(*day, tzinfo=WARSAW)
    iso = lambda h: (base + timedelta(hours=h)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"start": iso(h1), "end": iso(h2), "duration_hours": h2 - h1, "is_consecutive": True}


def assert_valid_schedule(slots):
    mins = slot_minutes(slots)
    assert len(slots) == 6
    assert [s["slot"] for s in slots] == [1, 2, 3, 4, 5, 6]
    assert all(0 <= m < 1440 for m in mins)
    assert all(mins[i] < mins[i + 1] for i in range(5)), mins


def test_charge_current_base_ignores_live_reg108_value():
    """A3: a lowered live Reg 108 (50 A) must not scale the base; SOC 50->90 of 16.13 kWh needs min_h = 2 at the
    100 A ceiling -> 4h window -> 100*2/4 = 50 A, base stays 100 A."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=50, max_soc=90, min_soc=20, best_pb_window=win(9, 13), best_sell_window=win(17, 20), max_charge_current=50.0,
    )
    charge = [s for s in slots if s["grid_charge"]][0]
    assert charge["target_charge_current_a"] == 50
    assert charge["base_charge_current_a"] == 100


def test_reg108_does_not_ratchet_across_repeated_syncs(tmp_path, monkeypatch):
    """A3: repeated syncs feeding back the previously written Reg 108 (and legacy --max-charge-current) never decrease it."""
    cache_dir = str(tmp_path)
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), {
        "_warsaw_date": "2026-10-17", "powerbank_best_window": win(9, 13), "best_sell_window": win(17, 20),
    })
    freeze_now(monkeypatch, datetime(2026, 10, 17, 10, 0, tzinfo=WARSAW))  # inside the 4h window
    written = [50]
    client = mock.MagicMock()
    client.get_configured_inverter_ip.return_value = "127.0.0.1"
    client.get_configured_inverter_port.return_value = 8899
    client.read_inverter_telemetry.side_effect = lambda **kw: {
        "success": True, "config": {"max_charge_current": written[-1]}, "telemetry": {"battery_soc": 50}, "active_tou": {}}
    client.sync_tou_schedule.return_value = {"success": True}

    def _set(host=None, port=None, current_a=None):
        written.append(current_a)
        return {"success": True}
    client.set_charge_current.side_effect = _set
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        for _ in range(4):
            pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir, max_charge_current=written[-1], force=True)
    assert written[1:] == [50, 50, 50, 50]

    # Daytime outside the window restores the 100 A ceiling, not the live/CLI value
    freeze_now(monkeypatch, datetime(2026, 10, 17, 15, 0, tzinfo=WARSAW))
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        res = pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir, max_charge_current=50, force=True)
    assert res["charge_current_modulation"]["desired_a"] == 100


def _modulate(monkeypatch, slots, hh, mm, next_cycle=None):
    freeze_now(monkeypatch, datetime(2026, 10, 17, hh, mm, tzinfo=WARSAW))
    client = mock.MagicMock()
    client.set_charge_current.return_value = {"success": True}
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        return pstryk_engine.check_and_apply_charge_current_modulation(slots=slots, next_cycle_target_a=next_cycle)["desired_a"]


def test_modulation_night_branches_keep_next_cycle_target(monkeypatch):
    """B4: 00:00 -> night window start keeps the pre-set target instead of resetting to 100 A."""
    # SOC 50 -> 90 of 16.13 kWh: min_h = 2 at the 100 A ceiling -> 4h window -> 50 A
    slots = pstryk_engine.generate_deye_tou_schedule(current_soc=50, max_soc=90, min_soc=20, best_pb_window=win(1, 5), best_sell_window=win(17, 20))
    assert [s for s in slots if s["grid_charge"]][0]["target_charge_current_a"] == 50
    assert _modulate(monkeypatch, slots, 0, 30, next_cycle=50) == 50   # gap before window (was 100)
    assert _modulate(monkeypatch, slots, 3, 0, next_cycle=80) == 50    # inside window
    # no clock switch: the next-cycle target applies as soon as today's last charge window has ended
    assert _modulate(monkeypatch, slots, 12, 0, next_cycle=50) == 50
    assert _modulate(monkeypatch, slots, 22, 0, next_cycle=67) == 67   # after today's charge: next cycle pre-set


def test_modulation_window_wrapping_midnight(monkeypatch):
    """B4: a charge slot ending at 00:00 (20-24) is not treated as 'already passed' in the evening."""
    slots = pstryk_engine.generate_deye_tou_schedule(current_soc=50, max_soc=90, min_soc=20, best_pb_window=win(20, 24), best_sell_window=win(17, 20))
    assert_valid_schedule(slots)
    assert _modulate(monkeypatch, slots, 19, 30, next_cycle=80) == 50  # before tonight's window (was 80)
    assert _modulate(monkeypatch, slots, 23, 30, next_cycle=80) == 50  # inside window


@pytest.mark.parametrize("pb,sell", [((19, 21), (17, 20)), ((22, 24), (17, 20)), ((20, 24), (17, 21))])
def test_schedule_evening_and_night_charge_windows_ordered(pb, sell):
    """B1: evening/night charge windows produce a valid strictly ordered schedule with the charge slot at window start."""
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(*pb), best_sell_window=win(*sell))
    assert_valid_schedule(slots)
    charge = [s for s in slots if s["grid_charge"]]
    assert charge and charge[0]["time"] == f"{pb[0]:02d}:00"
    # Evening peak discharge happens before the charge window (a lone peak is named by order, not by clock)
    peak = [s for s in slots if s["role"] == "peak"][0]
    assert slot_minutes([peak])[0] < pb[0] * 60


@pytest.mark.parametrize("pb", [(2, 4), (13, 15)])
def test_schedule_sell_window_ending_at_midnight(pb):
    """B1: a sell window ending 24:00 must not produce an out-of-order slot 6 at 00:00."""
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(*pb), best_sell_window=win(21, 24))
    assert_valid_schedule(slots)
    assert [s for s in slots if s["grid_charge"]][0]["time"] == f"{pb[0]:02d}:00"
    peak = [s for s in slots if s["time"] == "21:00"][0]
    assert peak["target_soc"] == 20


@pytest.mark.parametrize("pb", [(2, 4), (13, 15)])
def test_schedule_morning_sell_window_keeps_evening_peak(pb):
    """
    B6 (rewritten, no clock-invented 17:00 evening peak): a morning sell window places its discharge slot at 07:00
    (min SOC) and, with no other price peak known, drains to min SOC after it until the next charge. The only
    peak slot is the supplied window; the charge slot starts exactly at the charge window.
    """
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=20, best_pb_window=win(*pb), best_sell_window=win(7, 10))
    assert_valid_schedule(slots)
    by_time = {s["time"]: s for s in slots}
    assert by_time["07:00"]["target_soc"] == 20 and not by_time["07:00"]["grid_charge"]
    assert by_time["10:00"]["target_soc"] == 20
    assert [s["time"] for s in slots if s["role"] == "peak"] == ["07:00"]
    charge_m = slot_minutes([s for s in slots if s["grid_charge"]])[0]
    assert charge_m == pb[0] * 60


def test_slots_carry_nominal_voltage_for_cloud_write():
    # no hardcoded fallback peak: the schedule needs price data
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=20, nominal_voltage=51.2, pricing_data={"frames": warsaw_day_frames(datetime(2026, 10, 17).date(), lambda h: BASE_CURVE[h])})
    assert all(s["voltage"] == 51.2 for s in slots)


def _consolidated_mock(frames, calls=None):
    def _fetch(url, params, api_key, timeout=10):
        if calls is not None:
            calls.append(params)
        if params.get("temporal") == "latest":
            return {"frames": frames[:1]}
        if params.get("resolution") in ("day", "month"):
            return {"frames": []}
        ws, we = params.get("window_start"), params.get("window_end")
        return {"frames": [f for f in frames if ws <= f["start"] < we]}
    return _fetch


def test_today_window_starts_at_warsaw_midnight_saturday(monkeypatch, tmp_path):
    """B3: at Saturday 00:30 Warsaw, today's query starts at Warsaw midnight (Fri 22:00Z), not UTC midnight."""
    freeze_now(monkeypatch, datetime(2026, 10, 24, 0, 30, tzinfo=WARSAW))
    sat = datetime(2026, 10, 24).date()
    frames = warsaw_day_frames(sat) + warsaw_day_frames(sat + timedelta(days=1))
    calls = []
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames, calls)):
        res = pstryk_engine.fetch_consolidated_data(api_key="sk-T", use_cache=False, cache_dir=str(tmp_path))
    today_q = [c for c in calls if c.get("metrics") == "meter_values,cost,pricing,carbon"][0]
    assert today_q["window_start"] == "2026-10-23T22:00:00Z"
    assert res["price_coverage_complete"] is True
    assert len(res["deye_tou_schedule_today"]) == 6
    # B7: Saturday in Warsaw -> EV auto = 4h
    assert res["ev_best_window_today"]["duration_hours"] == 4


def test_ev_weekday_uses_warsaw_calendar():
    """B7: frames starting Fri 22:00Z are Saturday in Warsaw -> weekend 4h window."""
    frames = warsaw_day_frames(datetime(2026, 10, 24).date())
    assert frames[0]["start"] == "2026-10-23T22:00:00Z"
    assert pstryk_engine.find_ev_best_window({"frames": frames}, target_hours="auto")["duration_hours"] == 4


def test_incomplete_price_coverage_blocks_tou_and_cache(monkeypatch, tmp_path):
    """B5: missing forward hours -> no TOU, no cache write, and sync refuses to write."""
    freeze_now(monkeypatch, datetime(2026, 10, 21, 12, 30, tzinfo=WARSAW))
    day = datetime(2026, 10, 21).date()
    frames = warsaw_day_frames(day, skip_hours=range(13, 24))
    cache_dir = str(tmp_path)
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames)):
        res = pstryk_engine.fetch_consolidated_data(api_key="sk-T", use_cache=True, cache_dir=cache_dir)
    assert res["price_coverage_complete"] is False
    assert res["deye_tou_schedule_today"] == []
    assert "tou_error" in res
    assert not os.path.exists(pstryk_engine.get_cache_file_path("dol", cache_dir))

    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {}}
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames)), \
         mock.patch("pstryk_engine.resolve_api_key", return_value=("sk-T", "test")), \
         mock.patch("pstryk_engine.solarman_v5_client", client):
        sync = pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir)
    assert sync["success"] is False
    assert "Niepełne" in sync["error"]
    client.sync_tou_schedule.assert_not_called()
    client.set_charge_current.assert_not_called()


@pytest.mark.parametrize("raw,default,expected", [
    ("3h", 3, 3), ("4", 3, 4), (5, 3, 5), ("Auto 2h/4h", "auto", "auto"), ("auto", "auto", "auto"),
    ("Auto", 3, 3), ("", 3, 3), (None, "auto", "auto"), ("garbage", 3, 3), ("0h", 3, 3),
])
def test_parse_target_hours(raw, default, expected):
    assert pstryk_engine.parse_target_hours(raw, default) == expected


def test_cli_target_hours_threaded_to_both_modes():
    """A4: --sell-target-hours / --ev-target-hours accept raw input_select values in both modes."""
    with mock.patch("pstryk_engine.resolve_api_key", return_value=("sk-T", "test")), \
         mock.patch("pstryk_engine.fetch_consolidated_data", return_value={}) as mock_fetch:
        pstryk_engine.main(["--sell-target-hours", "4h", "--ev-target-hours", "Auto 2h/4h"])
    assert mock_fetch.call_args.kwargs["sell_target_hours"] == 4
    assert mock_fetch.call_args.kwargs["ev_target_hours"] == "auto"
    with mock.patch("pstryk_engine.sync_deye_inverter_tou_schedule", return_value={"success": True}) as mock_sync:
        pstryk_engine.main(["--sync-deye-tou", "--sell-target-hours", "2h", "--ev-target-hours", "3h"])
    assert mock_sync.call_args.kwargs["sell_target_hours"] == 2
    assert mock_sync.call_args.kwargs["ev_target_hours"] == 3
    with mock.patch("pstryk_engine.resolve_api_key", return_value=("sk-T", "test")), \
         mock.patch("pstryk_engine.fetch_consolidated_data", return_value={}) as mock_fetch:
        pstryk_engine.main([])
    assert mock_fetch.call_args.kwargs["sell_target_hours"] == energy_defaults.SELL_TARGET_HOURS
    assert mock_fetch.call_args.kwargs["ev_target_hours"] == "auto"


def test_target_hours_applied_to_windows_and_cache(monkeypatch, tmp_path):
    """A4: hours reach find_best_sell_window / find_ev_best_window; a cache built with other hours is not reused."""
    freeze_now(monkeypatch, datetime(2026, 10, 21, 0, 30, tzinfo=WARSAW))
    day = datetime(2026, 10, 21).date()
    frames = warsaw_day_frames(day) + warsaw_day_frames(day + timedelta(days=1))
    cache_dir = str(tmp_path)
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames)):
        r1 = pstryk_engine.fetch_consolidated_data(api_key="sk-T", cache_dir=cache_dir, sell_target_hours=2, ev_target_hours=3)
        r2 = pstryk_engine.fetch_consolidated_data(api_key="sk-T", cache_dir=cache_dir, sell_target_hours=2, ev_target_hours=3)
        r3 = pstryk_engine.fetch_consolidated_data(api_key="sk-T", cache_dir=cache_dir, sell_target_hours=4, ev_target_hours="auto")
    assert r1["best_sell_window_today"]["duration_hours"] == 2
    assert r1["ev_best_window_today"]["duration_hours"] == 3
    assert r2.get("_cache_hit") is True
    assert r3.get("_cache_hit") is not True
    assert r3["best_sell_window_today"]["duration_hours"] == 4
    assert r3["ev_best_window_today"]["duration_hours"] == 2  # Wednesday auto


def test_set_charge_current_cloud_fallback(monkeypatch):
    """B8: cloud fallback runs when the local client raises or reports cloud_attempted False, at most once otherwise."""
    import solarman_v5_client
    import deye_cloud_client
    cloud = mock.MagicMock(return_value={"success": True, "source": "cloud"})
    monkeypatch.setattr(deye_cloud_client, "set_charge_current_cloud", cloud)

    monkeypatch.setattr(solarman_v5_client, "set_charge_current", mock.MagicMock(side_effect=OSError("down")))
    assert pstryk_engine.main(["--set-charge-current", "60"]) == 0
    assert cloud.call_count == 1

    monkeypatch.setattr(solarman_v5_client, "set_charge_current", mock.MagicMock(return_value={"success": False, "cloud_attempted": False}))
    assert pstryk_engine.main(["--restore-charge-current"]) == 0
    assert cloud.call_count == 2
    assert cloud.call_args.kwargs["current_a"] == 100

    # Client already tried the cloud internally (its default on failure) -> no second cloud call
    monkeypatch.setattr(solarman_v5_client, "set_charge_current", mock.MagicMock(return_value={"success": False}))
    assert pstryk_engine.main(["--set-charge-current", "60"]) == 1
    assert cloud.call_count == 2


def test_tou_sync_does_not_double_call_cloud(tmp_path):
    """Coordinator item: solarman sync_tou_schedule already falls back to cloud; engine must not call cloud again."""
    import deye_cloud_client
    cache_dir = str(tmp_path)
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), {
        "_warsaw_date": warsaw_today_iso(), "powerbank_best_window": win(13, 15), "best_sell_window": win(17, 20)})
    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {}}
    client.sync_tou_schedule.return_value = {"success": False, "error": "local and cloud failed"}
    client.set_charge_current.return_value = {"success": True}
    with mock.patch("pstryk_engine.solarman_v5_client", client), \
         mock.patch.object(deye_cloud_client, "sync_tou_schedule") as cloud_sync:
        res = pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir, force=True)
    assert res["success"] is False
    cloud_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Code-review round: H1 / M1 / M2 / M3 / M6 / L5 / L7
# ---------------------------------------------------------------------------

STRETCH_LABELS = ("Czuwanie po Ładowaniu (Hold do Szczytu)", "Autokonsumpcja do Min SOC (przed Ładowaniem)")


def _hm(m):
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def test_schedule_property_all_windows():
    """
    H1/M6 property test over every (charge start 0-23, duration 1-6) x (sell start 0-23, duration 1-5)
    x calibration: 6 strictly increasing slots, SOC within [min, max], every charge hour inside a grid-charge
    slot, and every sell hour (not overlapping the charge window) inside a min-SOC discharge slot.
    """
    min_soc, max_soc = 20, 90
    failures = []
    for calib in (False, True):
        top = 100 if calib else max_soc
        for ps in range(24):
            for pd in range(1, 7):
                pe = ps + pd
                if pe > 24:
                    continue
                for ss in range(24):
                    for sd in range(1, 6):
                        se = ss + sd
                        if se > 24:
                            continue
                        slots = pstryk_engine.generate_deye_tou_schedule(
                            max_soc=max_soc, min_soc=min_soc, calibration_active=calib,
                            best_pb_window={"start": _hm(ps * 60), "end": _hm(pe * 60), "duration_hours": pd},
                            best_sell_window={"start": _hm(ss * 60), "end": _hm(se * 60)},
                            max_discharge_current=140, nominal_voltage=51.2, max_charge_current=100,
                        )
                        ts = slot_minutes(slots)
                        key = (calib, ps, pd, ss, sd)
                        if len(slots) != 6 or any(ts[i] >= ts[i + 1] for i in range(5)) or not all(0 <= t < 1440 for t in ts):
                            failures.append((key, "order", ts))
                            continue
                        if any(not (min_soc <= s["target_soc"] <= top) for s in slots):
                            failures.append((key, "soc", [s["target_soc"] for s in slots]))

                        def active(m):
                            idx = [i for i, t in enumerate(ts) if t <= m]
                            return slots[idx[-1] if idx else 5]

                        if any(not active(h * 60)["grid_charge"] for h in range(ps, pe)):
                            failures.append((key, "charge", ts))
                        if not (ss < pe and se > ps):
                            if any(active(h * 60)["target_soc"] != min_soc or active(h * 60)["grid_charge"] for h in range(ss, se)):
                                failures.append((key, "sell", ts))
                        # (a) after a charge, until the next peak: hold max; (b) after a peak, until the next charge: min
                        kinds = [x["role"] for x in slots]
                        assert all((k == "charge") == x["grid_charge"] for k, x in zip(kinds, slots))
                        for i, k in enumerate(kinds):
                            if k in ("charge", "peak"):
                                continue
                            # S3: head of the day (no charge/peak earlier today) keeps the branch's own SOC
                            prevs = [kinds[j] for j in range(i - 1, -1, -1) if kinds[j] in ("charge", "peak")]
                            if not prevs:
                                if slots[i]["label"] in STRETCH_LABELS:
                                    failures.append((key, "head-wrapped", ts))
                                continue
                            prev_k = prevs[0]
                            next_k = next(kinds[(i + j) % 6] for j in range(1, 7) if kinds[(i + j) % 6] in ("charge", "peak"))
                            if prev_k == "charge" and next_k == "peak" and not (780 <= ts[i] < 1320) and slots[i]["target_soc"] != top:
                                failures.append((key, "hold-after-charge", ts, [x["target_soc"] for x in slots]))
                            if prev_k == "peak" and next_k == "charge" and slots[i]["target_soc"] != min_soc:
                                failures.append((key, "min-before-charge", ts, [x["target_soc"] for x in slots]))
    assert not failures, f"{len(failures)} failures, e.g. {failures[:5]}"


def test_h1_charge_survives_trimmed_sell_collision():
    """H1 repro: charge 01-03 + sell 00-01 must keep a grid-charge slot at 01:00."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=20, best_pb_window={"start": "01:00", "end": "03:00", "duration_hours": 2},
        best_sell_window={"start": "00:00", "end": "01:00"})
    assert [s["time"] for s in slots if s["grid_charge"]][:1] == ["01:00"]


def test_m6_discharge_starts_at_sell_start():
    """M6 repro: charge 17-18 + sell 18-19 -> discharge slot at 18:00, not 19:00."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=20, best_pb_window={"start": "17:00", "end": "18:00", "duration_hours": 1},
        best_sell_window={"start": "18:00", "end": "19:00"})
    by_time = {s["time"]: s for s in slots}
    assert by_time["17:00"]["grid_charge"] is True
    assert by_time["18:00"]["target_soc"] == 20 and not by_time["18:00"]["grid_charge"]


def test_m1_charge_ceiling_is_hard_capped_at_100(monkeypatch):
    import importlib
    try:
        monkeypatch.setenv("BATTERY_MAX_CHARGE_CURRENT", "140")
        monkeypatch.setenv("BATTERY_CHARGE_CURRENT_A", "140")
        importlib.reload(energy_defaults)
        assert energy_defaults.FALLBACK_BATTERY_MAX_CHARGE_CURRENT == 140.0
        assert energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A == 100.0
    finally:
        monkeypatch.undo()
        importlib.reload(energy_defaults)
    assert energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A <= 100.0


@pytest.mark.parametrize("day,skip,expected", [
    ((2026, 10, 25), (), True),                         # DST end: 25 frames
    ((2026, 10, 25), "drop_one", True),                 # DST end: API returns only 24 frames
    ((2026, 3, 29), (), True),                          # DST start: 23 frames
    ((2026, 10, 21), (), True),                         # normal 24 frames
    ((2026, 10, 21), range(9, 12), False),              # 3h gap
    ((2026, 10, 21), range(20, 24), False),             # missing tail
    ((2026, 10, 21), (0,), False),                      # missing start
])
def test_m2_span_based_day_coverage(day, skip, expected):
    d = datetime(*day).date()
    frames = warsaw_day_frames(d, skip_hours=() if skip == "drop_one" else skip)
    if skip == "drop_one":
        frames = frames[:2] + frames[3:]  # one of the doubled 02:00 hours missing
        assert len(frames) == 24
    assert pstryk_engine.warsaw_day_covered(frames, d) is expected


def test_m2_dst_day_with_24_frames_still_computes_tou(monkeypatch, tmp_path):
    freeze_now(monkeypatch, datetime(2026, 10, 25, 0, 30, tzinfo=WARSAW))
    d = datetime(2026, 10, 25).date()
    today = warsaw_day_frames(d)
    frames = today[:2] + today[3:] + warsaw_day_frames(d + timedelta(days=1))
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames)):
        res = pstryk_engine.fetch_consolidated_data(api_key="sk-T", use_cache=False, cache_dir=str(tmp_path))
    assert res["price_coverage_complete"] is True
    assert len(res["deye_tou_schedule_today"]) == 6
    assert res["has_tomorrow_pricing"] is True


def test_m3_no_next_cycle_preset_before_evening(monkeypatch):
    """M3 (rewritten, no 19:00 clock switch): the next-cycle target applies exactly once today's last charge window has ended."""
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(1, 5), best_sell_window=win(17, 20))
    assert _modulate(monkeypatch, slots, 4, 59, next_cycle=70) != 70      # still inside today's window
    assert _modulate(monkeypatch, slots, 5, 2, next_cycle=50) == 50      # window ended -> next cycle
    slots_24 = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(2, 4), best_sell_window=win(17, 20))
    assert _modulate(monkeypatch, slots_24, 4, 0, next_cycle=50) == 50
    assert _modulate(monkeypatch, slots_24, 18, 59, next_cycle=50) == 50  # same just before and
    assert _modulate(monkeypatch, slots_24, 19, 0, next_cycle=50) == 50   # just after the old 19:00 boundary


def test_l5_cache_from_previous_warsaw_day_is_a_miss(monkeypatch, tmp_path):
    """L5: at 00:00:30 a cache written yesterday (still within TTL) must not be reused."""
    cache_dir = str(tmp_path)
    d = datetime(2026, 10, 21).date()
    frames = warsaw_day_frames(d) + warsaw_day_frames(d + timedelta(days=1)) + warsaw_day_frames(d + timedelta(days=2))
    freeze_now(monkeypatch, datetime(2026, 10, 21, 23, 59, 50, tzinfo=WARSAW))
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames)):
        r1 = pstryk_engine.fetch_consolidated_data(api_key="sk-T", cache_dir=cache_dir)
        assert r1["_warsaw_date"] == "2026-10-21"
        freeze_now(monkeypatch, datetime(2026, 10, 22, 0, 0, 30, tzinfo=WARSAW))
        r2 = pstryk_engine.fetch_consolidated_data(api_key="sk-T", cache_dir=cache_dir)
    assert r2.get("_cache_hit") is not True
    assert r2["_warsaw_date"] == "2026-10-22"


def test_l7_next_cycle_target_from_fresh_data(monkeypatch, tmp_path):
    """L7: with no usable cache, the next-cycle Reg 108 target comes from the freshly fetched payload."""
    freeze_now(monkeypatch, datetime(2026, 10, 17, 22, 0, tzinfo=WARSAW))
    fresh = {
        "price_coverage_complete": True,
        "powerbank_best_window": win(2, 4), "best_sell_window": win(17, 20),
        "deye_tou_schedule_tomorrow": [{"grid_charge": True, "target_charge_current_a": 67}],
    }
    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {}}
    client.sync_tou_schedule.return_value = {"success": True}
    client.set_charge_current.return_value = {"success": True}
    with mock.patch("pstryk_engine.solarman_v5_client", client), \
         mock.patch("pstryk_engine.resolve_api_key", return_value=("sk-T", "test")), \
         mock.patch("pstryk_engine.fetch_consolidated_data", return_value=fresh):
        res = pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=str(tmp_path), force=True)
    assert res["charge_current_modulation"]["desired_a"] == 67


@pytest.mark.parametrize("strip_end", [False, True])
def test_m2_total_missing_hours_capped_at_one(strip_end):
    """M2': isolated 1h holes add up; every other hour missing must fail, one hole in total is tolerated."""
    d = datetime(2026, 10, 21).date()

    def frames_for(skip):
        fr = warsaw_day_frames(d, skip_hours=skip)
        if strip_end:
            for f in fr:
                f.pop("end")
        return fr

    assert pstryk_engine.warsaw_day_covered(frames_for(range(1, 24, 2)), d) is False   # 12 of 24 missing
    assert pstryk_engine.warsaw_day_covered(frames_for((7, 15)), d) is False            # 2 separate holes
    assert pstryk_engine.warsaw_day_covered(frames_for((12,)), d) is True               # documented 1h tolerance
    assert pstryk_engine.warsaw_day_covered(frames_for((23,)), d) is False              # last hour must be covered
    dst = datetime(2026, 10, 25).date()
    fr = warsaw_day_frames(dst)
    fr = fr[:2] + fr[3:]
    if strip_end:
        for f in fr:
            f.pop("end")
    assert len(fr) == 24 and pstryk_engine.warsaw_day_covered(fr, dst) is True          # 25h DST day as 24 frames


def _sched(slots):
    return [(s["time"], s["target_soc"], s["grid_charge"]) for s in slots]


def test_live_bug_sell_before_charge_holds_after_charge_and_drains_before_charge():
    """
    Live repro (min 10, max 90, margins 10/10, sell 06-09, charge 13-15):
    After 13:00-15:00 charge, prices at 15:00 are elevated, so autokonsumpcja starts immediately at 15:00 (target 10%).
    (a) 15:00 starts autokonsumpcja down to min_soc; (b) 09:00 is min_soc before cheap refill;
    (c) 00:00 keeps morning reserve before 06:00 sale.
    """
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=10,
        best_pb_window={"start": "13:00", "end": "15:00", "duration_hours": 2},
        best_sell_window={"start": "06:00", "end": "09:00"})
    # (rewritten) without price frames 15:00 is not known to be elevated: hold 90% from the charge until midnight;
    # 19:00 is the split of that hold slot. The 00:00 slot is the projected SOC at 00:00 (current SOC at the
    # pinned midnight clock = DEFAULT_CURRENT_SOC 85), never the evening hold copied over midnight
    assert _sched(slots) == [
        ("00:00", energy_defaults.DEFAULT_CURRENT_SOC, False), ("06:00", 10, False), ("09:00", 10, False),
        ("13:00", 90, True), ("15:00", 90, False), ("19:00", 90, False),
    ]


def test_midday_charge_13_15_sell_17_20_immediate_autoconsumption_and_min_post_peak():
    """Midday charge 13-15 with evening peak: immediate autokonsumpcja at 15:00 (no 90% hold during high prices) and min_soc post peak."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=10,
        best_pb_window={"start": "13:00", "end": "15:00", "duration_hours": 2},
        best_sell_window={"start": "17:00", "end": "20:00"})
    # (rewritten) 15-17 is not a price peak without price frames -> hold 90% until the 17:00 peak;
    # 00:00 (and its 06:00 split) drain to min_soc after the previous peak, before the 13:00 charge
    assert _sched(slots) == [
        ("00:00", 10, False), ("06:00", 10, False), ("13:00", 90, True),
        ("15:00", 90, False), ("17:00", 10, False), ("20:00", 10, False),
    ]


def _ref(pb, sell, **kw):
    hm = lambda h: f"{h % 24:02d}:00"
    params = dict(max_soc=90, min_soc=10)
    params.update(kw)
    return pstryk_engine.generate_deye_tou_schedule(
        best_pb_window={"start": hm(pb[0]), "end": hm(pb[1]), "duration_hours": pb[1] - pb[0]},
        best_sell_window={"start": hm(sell[0]), "end": hm(sell[1])}, **params)


def test_s2_classification_uses_roles_not_labels():
    """S2: renaming every label (e.g. "Szczyt ..." -> "XSzczyt ...") must not change any target SOC."""
    import importlib.util
    src = open(pstryk_engine.__file__, encoding="utf-8").read()
    renamed_src = src.replace('"Szczyt', '"XSzczyt').replace('f"Szczyt', 'f"XSzczyt')
    assert renamed_src != src
    spec = importlib.util.spec_from_loader("pstryk_engine_renamed", loader=None)
    renamed = importlib.util.module_from_spec(spec)
    renamed.__file__ = pstryk_engine.__file__
    exec(compile(renamed_src, pstryk_engine.__file__, "exec"), renamed.__dict__)
    for pb in [(2, 4), (13, 15), (19, 21), (22, 24), (9, 13)]:
        for sell in [(6, 9), (7, 10), (12, 15), (17, 20), (21, 24)]:
            kw = dict(max_soc=90, min_soc=10, now_dt=pstryk_engine.datetime.now(WARSAW),
                      best_pb_window={"start": f"{pb[0]:02d}:00", "end": f"{pb[1] % 24:02d}:00", "duration_hours": pb[1] - pb[0]},
                      best_sell_window={"start": f"{sell[0]:02d}:00", "end": f"{sell[1] % 24:02d}:00"})
            a = pstryk_engine.generate_deye_tou_schedule(**kw)
            b = renamed.generate_deye_tou_schedule(**kw)
            assert [(x["time"], x["target_soc"], x["grid_charge"]) for x in a] == \
                   [(x["time"], x["target_soc"], x["grid_charge"]) for x in b], (pb, sell)
            assert any(x["label"].startswith("XSzczyt") for x in b)


def test_s1_evening_charge_19_21_reference_schedule():
    slots = _ref((19, 21), (17, 19))
    # (rewritten) no 08:00 "morning boundary": the evening charge is held until the next price peak (17:00);
    # the 00:00 slot holds the projected SOC at 00:00 (current SOC), not the evening hold copied over midnight
    assert _sched(slots) == [
        ("00:00", energy_defaults.DEFAULT_CURRENT_SOC, False), ("08:00", energy_defaults.DEFAULT_CURRENT_SOC, False),
        ("12:00", energy_defaults.DEFAULT_CURRENT_SOC, False),
        ("17:00", 10, False), ("19:00", 90, True), ("21:00", 90, False),
    ]


@pytest.mark.parametrize("pb,sell", [((19, 21), (17, 19)), ((22, 24), (17, 20)), ((20, 24), (17, 20)), ((19, 21), (7, 10))])
def test_s1_evening_charge_hold_ends_at_morning_boundary(pb, sell):
    """
    S1 (rewritten, no clock "morning boundary"): an evening/late charge is held at Max SOC from the end of the
    charge window until midnight; after midnight the table's own head slot holds the projected SOC at 00:00 (the
    current SOC, 70 here) until the next price peak starts, and the peak discharges to Min SOC.
    """
    slots = _ref(pb, sell, current_soc=70)
    assert_valid_schedule(slots)
    mins = slot_minutes(slots)
    active_at = lambda m: slots[max([i for i, t in enumerate(mins) if t <= m % 1440], default=5)]
    m = pb[1] * 60
    while m % 1440 != sell[0] * 60:
        want = 90 if m < 1440 else 70
        assert active_at(m)["target_soc"] == want and not active_at(m)["grid_charge"], (m, active_at(m))
        m += 60
    assert active_at(sell[0] * 60)["target_soc"] == 10 and active_at(sell[0] * 60)["role"] == "peak"


def test_s3_late_charge_midnight_slot_is_set_by_branch_not_wrap():
    """S3: the 00:00 slot of a 22-24 charge comes from the branch itself, not from wrapping 'prev' to today's charge."""
    s0 = _ref((22, 24), (17, 20))[0]
    assert s0["time"] == "00:00"
    assert s0["label"] not in STRETCH_LABELS
    assert s0["role"] != "charge"

def test_midday_charge_elevated_price_starts_immediate_discharge():
    """
    Verifies that when midday charge finishes at 15:00 and retail price is elevated (e.g. 1.48 zł/kWh),
    autokonsumpcja starts immediately at 15:00 with target_soc = safe_min_soc (10%).
    Ensures that no wasteful 90% hold is created between 15:00 and 17:00.
    """
    frames = []
    # Build 24 hours of mock pricing: cheap at 13-15 (0.84), high at 15-21 (1.48 to 1.60), normal elsewhere (1.10)
    for h in range(24):
        p = 0.84 if 13 <= h < 15 else (1.55 if 17 <= h < 21 else (1.48 if 15 <= h < 17 else 1.10))
        frames.append({
            "start": f"2026-10-21T{h:02d}:00:00+02:00",
            "end": f"2026-10-21{(h+1)%24:02d}:00:00+02:00",
            "metrics": {"pricing": {"price_gross": p, "price_net": p / 1.23}},
        })
    pricing = {"frames": frames}

    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90,
        min_soc=10,
        best_pb_window={"start": "13:00", "end": "15:00", "duration_hours": 2, "average_price": 0.84},
        best_sell_window={"start": "17:00", "end": "21:00"},
        pricing_data=pricing,
    )

    sched = [(s["time"], s["target_soc"], s["grid_charge"]) for s in slots]
    # Slot at 15:00 must start discharging immediately down to 10% (no 90% hold)
    slot_15 = [s for s in slots if s["time"] == "15:00"][0]
    assert slot_15["target_soc"] == 10
    assert slot_15["grid_charge"] is False
    # Post-peak slot at 21:00 must stay strictly at min_soc (10%), not at morning reserve (20%)
    slot_21 = [s for s in slots if s["time"] == "21:00"][0]
    assert slot_21["target_soc"] == 10
    assert slot_21["grid_charge"] is False
    # No hold at 90% exists after charging
    holds_at_90 = [s for s in slots if s["target_soc"] == 90 and not s["grid_charge"]]
    assert len(holds_at_90) == 0


def test_post_peak_standby_strictly_uses_min_soc():
    """
    Verifies that after the evening peak has passed, post-peak evening and night standby
    strictly targets safe_min_soc (10%), rejecting unnecessary 20% morning reserve hold.
    """
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90,
        min_soc=10,
        best_pb_window={"start": "02:00", "end": "05:00", "duration_hours": 3},
        best_sell_window={"start": "17:00", "end": "21:00"},
    )
    last_slot = slots[-1]
    assert last_slot["time"] == "21:00"
    assert last_slot["target_soc"] == 10
    assert last_slot["grid_charge"] is False


def test_night_charge_cheap_hour_preserves_hold_until_peak():
    """
    Verifies that when night charge ends at 05:00 and hours 05:00-07:00 are cheap,
    the 90% hold is preserved to protect battery energy for the expensive morning peak at 07:00.
    """
    frames = []
    for h in range(24):
        # 03-05 cheap (0.86), 05-07 still cheap (0.88), 07-10 morning peak (1.85), normal (1.20)
        p = 0.86 if 3 <= h < 5 else (0.88 if 5 <= h < 7 else (1.85 if 7 <= h < 10 else 1.20))
        frames.append({
            "start": f"2026-10-22T{h:02d}:00:00+02:00",
            "end": f"2026-10-22{(h+1)%24:02d}:00:00+02:00",
            "metrics": {"pricing": {"price_gross": p, "price_net": p / 1.23}},
        })
    pricing = {"frames": frames}

    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90,
        min_soc=10,
        best_pb_window={"start": "03:00", "end": "05:00", "duration_hours": 2, "average_price": 0.86},
        best_sell_window={"start": "07:00", "end": "10:00"},
        pricing_data=pricing,
    )

    slot_05 = [s for s in slots if s["time"] == "05:00"][0]
    # At 05:00, prices are cheap (0.88 < 0.86 * 1.15 and < mean 1.20), so 90% hold is preserved until 07:00 peak
    assert slot_05["target_soc"] == 90
    assert slot_05["grid_charge"] is False
    assert "Hold do Szczytu" in slot_05["label"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Data-derived TOU (no clock/peak/duration literals) -- TDD block for the plan
# try-to-continue-this-zippy-cloud.md.  These tests define the contract of the implementation.
#
# ASSUMED NEW SIGNATURES (implementer must match; all additions are keyword-only/optional):
#   analyse_day_price_profile(frames: List[dict]) -> dict
#       {"mean": float,
#        "segments": [ {kind: "valley"|"peak", start_m, end_m, avg, min, max, hours}, ... ]  # time ordered, contiguous,
#                                                                                         # Warsaw minutes since midnight,
#                                                                                         # end_m of the last hour = start_m + 60
#        "peaks": [ peak segment dicts ranked by gross price (avg) descending ]}
#       valley = hours with price_gross <  day mean, peak = hours with price_gross >= day mean.
#       `hours` is an int count or a list of hour frames. (A bare list of segments is also accepted by the tests.)
#   find_powerbank_best_window(data, target_hours="auto", allow_disjoint=False, now_dt=None, *,
#                              current_soc=None, max_soc=None, capacity_kwh=None, voltage=None,
#                              ceiling_a=None, loss_pct=None) -> existing dict (start/end UTC ISO, duration_hours, ...)
#   generate_deye_tou_schedule(...existing kwargs...) -> List[slot] | None
#       uses best_pb_window + pricing_data (+ best_discharge_windows); returns None when it has no pricing data
#       (no pricing_data, no best_discharge_windows, no best_sell_window).  Slot "target_charge_current_a" of the
#       grid-charge slot == clamp(round(ceiling*min_h/T), floor, ceiling).
#   check_and_apply_charge_current_modulation(slots, host, port, base_charge_current, next_cycle_target_a) unchanged;
#       next-cycle target applies once today's last charge window has ended (no clock), literal 100 -> ceiling.
#   BATTERY_CHARGE_CURRENT_MIN_FLOOR_A (energy_defaults, from .env.example) is the hardware current floor
#       (tests fall back to 20 A when it is not defined yet).
# ---------------------------------------------------------------------------

import math

ENERGY_KW = dict(current_soc=20, max_soc=90, capacity_kwh=10.24, voltage=51.2, ceiling_a=100.0, loss_pct=10.0)
_FLOOR_A = energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A


def _min_hours(current_soc=None):
    soc = ENERGY_KW["current_soc"] if current_soc is None else current_soc
    need = (ENERGY_KW["max_soc"] - soc) / 100.0 * ENERGY_KW["capacity_kwh"] * (1 + ENERGY_KW["loss_pct"] / 100.0)
    energy_h = max(1, math.ceil(need / (ENERGY_KW["ceiling_a"] * ENERGY_KW["voltage"] / 1000.0)))
    return max(int(energy_defaults.CHARGE_WINDOW_MIN_HOURS), energy_h)


def _expected_current(min_h, total_h):
    ceiling = ENERGY_KW["ceiling_a"]
    return int(max(_FLOOR_A, min(ceiling, round(ceiling * min_h / total_h))))


def _warsaw_m(iso):
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(WARSAW)
    return d.hour * 60 + d.minute


def _win_minutes(win_dict):
    """(start_m, end_m) of a window dict in Warsaw minutes; end 00:00 -> 1440."""
    start, end = _warsaw_m(win_dict["start"]), _warsaw_m(win_dict["end"])
    return start, (end if end > start else end + 1440)


def _segments(profile):
    return profile["segments"] if isinstance(profile, dict) else profile


def _valley_containing(frames, minute):
    for seg in _segments(pstryk_engine.analyse_day_price_profile(frames)):
        if seg["kind"] == "valley" and seg["start_m"] <= minute < seg["end_m"]:
            return seg
    raise AssertionError(f"no valley contains minute {minute}")


def _n_hours(seg):
    h = seg["hours"]
    return len(h) if isinstance(h, (list, tuple)) else h


def _tou_for(frames):
    """Full pipeline as the engine runs it: charge window + discharge windows + schedule from one price day."""
    data = {"frames": frames}
    pb = pstryk_engine.find_powerbank_best_window(data, target_hours="auto", **ENERGY_KW)
    dw = pstryk_engine.find_best_discharge_windows(data)
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=ENERGY_KW["current_soc"], max_soc=ENERGY_KW["max_soc"], min_soc=20,
        best_pb_window=None if "error" in pb else pb, best_discharge_windows=dw, pricing_data=data,
        max_charge_current=ENERGY_KW["ceiling_a"], max_discharge_current=140.0,
        battery_capacity_kwh=ENERGY_KW["capacity_kwh"], nominal_voltage=ENERGY_KW["voltage"],
        loss_buffer_pct=ENERGY_KW["loss_pct"],
    )
    return pb, dw, slots


# Synthetic day: flat edge segments (so a padded shift keeps every segment price identical).
# kinds: P(0-2) V(3-5) P(6-10) V(11-14) P(15-19) V(20-23); day mean ~1.44 separates V <= 1.25 from P >= 1.54.
BASE_CURVE = (
    [1.60] * 3 + [0.90, 0.86, 0.91] + [1.54, 1.86, 1.80, 1.73, 1.60]
    + [1.20, 1.15, 1.18, 1.25] + [1.62, 1.80, 1.95, 2.06, 2.01] + [1.10] * 4
)
TEST_DAY = datetime(2026, 10, 22).date()


def _curve_frames(prices, day=TEST_DAY):
    return warsaw_day_frames(day, lambda h: prices[h])


def _shifted(prices, k):
    """Shift the curve k hours later; the vacated start is padded with the (flat) first segment, the end truncated."""
    return [prices[max(h - k, 0)] for h in range(24)]


def test_analyse_day_price_profile_splits_at_day_mean():
    frames = _curve_frames(BASE_CURVE)
    prof = pstryk_engine.analyse_day_price_profile(frames)
    segs = _segments(prof)
    mean = sum(BASE_CURVE) / 24.0
    assert [s["kind"] for s in segs] == ["peak", "valley", "peak", "valley", "peak", "valley"]
    # contiguous, covering the whole Warsaw day
    assert segs[0]["start_m"] == 0 and segs[-1]["end_m"] == 1440
    assert all(segs[i]["end_m"] == segs[i + 1]["start_m"] for i in range(len(segs) - 1))
    for seg in segs:
        hrs = range(seg["start_m"] // 60, seg["end_m"] // 60)
        assert _n_hours(seg) == len(hrs)
        assert seg["avg"] == pytest.approx(sum(BASE_CURVE[h] for h in hrs) / len(hrs), abs=1e-3)
        assert all((BASE_CURVE[h] >= mean) == (seg["kind"] == "peak") for h in hrs)
    if isinstance(prof, dict) and "peaks" in prof:
        peaks = prof["peaks"]
        assert {(p["start_m"], p["end_m"]) for p in peaks} == {(s["start_m"], s["end_m"]) for s in segs if s["kind"] == "peak"}
        assert [p["avg"] for p in peaks] == sorted((p["avg"] for p in peaks), reverse=True)   # ranked by gross price


def test_analyse_day_price_profile_hour_equal_to_mean_is_peak():
    segs = _segments(pstryk_engine.analyse_day_price_profile(_curve_frames([1.0] * 24)))
    assert [s["kind"] for s in segs] == ["peak"]
    assert (segs[0]["start_m"], segs[0]["end_m"]) == (0, 1440)


@pytest.mark.parametrize("k", [1, 2, 3])
def test_shift_invariance_profile_charge_window_and_slots(k):
    base_frames, shifted_frames = _curve_frames(BASE_CURVE), _curve_frames(_shifted(BASE_CURVE, k))
    base_segs = _segments(pstryk_engine.analyse_day_price_profile(base_frames))
    shift_segs = _segments(pstryk_engine.analyse_day_price_profile(shifted_frames))
    assert [s["kind"] for s in shift_segs] == [s["kind"] for s in base_segs]
    last = len(base_segs) - 1
    for i, (b, r) in enumerate(zip(base_segs, shift_segs)):
        assert r["start_m"] == (0 if i == 0 else b["start_m"] + 60 * k)
        assert r["end_m"] == (1440 if i == last else b["end_m"] + 60 * k)
        assert r["avg"] == pytest.approx(b["avg"], abs=1e-3)

    pb0, dw0, slots0 = _tou_for(base_frames)
    pb1, dw1, slots1 = _tou_for(shifted_frames)
    s0, e0 = _win_minutes(pb0)
    assert _win_minutes(pb1) == (s0 + 60 * k, e0 + 60 * k)
    assert_valid_schedule(slots0)
    assert_valid_schedule(slots1)
    t0, t1 = slot_minutes(slots0), slot_minutes(slots1)
    assert t0[0] == t1[0] == 0                                   # day anchor is not a price event
    assert t1[1:] == [m + 60 * k for m in t0[1:]]
    assert [s["grid_charge"] for s in slots1] == [s["grid_charge"] for s in slots0]
    assert [s["target_soc"] for s in slots1] == [s["target_soc"] for s in slots0]


def test_mirror_profile_mirrors_valley_peak_and_charge_window():
    mirrored = list(reversed(BASE_CURVE))
    base_frames, mirror_frames = _curve_frames(BASE_CURVE), _curve_frames(mirrored)
    base_segs = _segments(pstryk_engine.analyse_day_price_profile(base_frames))
    mirror_segs = _segments(pstryk_engine.analyse_day_price_profile(mirror_frames))
    assert [(s["kind"], 1440 - s["end_m"], 1440 - s["start_m"]) for s in base_segs][::-1] == \
        [(s["kind"], s["start_m"], s["end_m"]) for s in mirror_segs]
    s, e = _win_minutes(pstryk_engine.find_powerbank_best_window({"frames": base_frames}, target_hours="auto", **ENERGY_KW))
    ms, me = _win_minutes(pstryk_engine.find_powerbank_best_window({"frames": mirror_frames}, target_hours="auto", **ENERGY_KW))
    assert (ms, me) == (1440 - e, 1440 - s)


@pytest.mark.parametrize("soc", [10, 20, 50, 80])
def test_charge_window_duration_from_energy_need_inside_valley(soc):
    kw = dict(ENERGY_KW, current_soc=soc)
    frames = _curve_frames(BASE_CURVE)
    res = pstryk_engine.find_powerbank_best_window({"frames": frames}, target_hours="auto", **kw)
    start, end = _win_minutes(res)
    assert res["duration_hours"] == (end - start) // 60 >= _min_hours(soc)
    valley = _valley_containing(frames, start)
    assert valley["start_m"] <= start and end <= valley["end_m"]        # never crosses the valley edge
    assert res["is_consecutive"] is True


def test_charge_window_identical_on_weekday_and_weekend():
    wed, sat = datetime(2026, 10, 21).date(), datetime(2026, 10, 24).date()
    res_wed = pstryk_engine.find_powerbank_best_window({"frames": _curve_frames(BASE_CURVE, wed)}, target_hours="auto", **ENERGY_KW)
    res_sat = pstryk_engine.find_powerbank_best_window({"frames": _curve_frames(BASE_CURVE, sat)}, target_hours="auto", **ENERGY_KW)
    assert _win_minutes(res_wed) == _win_minutes(res_sat)
    assert res_wed["duration_hours"] == res_sat["duration_hours"]


def test_charge_window_stretch_stops_at_floor_current():
    """Stretch never lowers ceiling*min_h/T below the hardware floor."""
    frames = _curve_frames([0.80 + 0.01 * h if h < 20 else 2.0 for h in range(24)])
    res = pstryk_engine.find_powerbank_best_window({"frames": frames}, target_hours="auto", **ENERGY_KW)
    assert ENERGY_KW["ceiling_a"] * _min_hours() / res["duration_hours"] >= _FLOOR_A - 1e-9 or res["duration_hours"] == _min_hours()


def test_charge_window_flat_long_valley_stops_at_floor_current():
    """Flat 20 h valley: window must stop at floor(ceiling*N/floor) hours, current stays >= floor."""
    frames = _curve_frames([0.50 if h < 20 else 2.0 for h in range(24)])
    res = pstryk_engine.find_powerbank_best_window({"frames": frames}, target_hours="auto", **ENERGY_KW)
    n, ceiling = res["min_hours"], ENERGY_KW["ceiling_a"]
    assert res["duration_hours"] <= math.floor(ceiling * n / _FLOOR_A)
    assert round(ceiling * n / res["duration_hours"]) >= _FLOOR_A


def test_modulation_unparsable_slot_time_does_not_open_midnight_window(monkeypatch):
    slots = [{"time": "garbage", "grid_charge": True, "target_charge_current_a": 40}]
    assert _modulate(monkeypatch, slots, 0, 30) == int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))


def test_charge_window_respects_user_minimum_hours():
    """N >= CHARGE_WINDOW_MIN_HOURS even when the energy need fits in far less than one hour."""
    frames = _curve_frames([0.50 if 2 <= h < 8 else 1.50 for h in range(24)])
    kw = dict(ENERGY_KW, current_soc=88)
    res = pstryk_engine.find_powerbank_best_window({"frames": frames}, target_hours="auto", **kw)
    assert res["min_hours"] == energy_defaults.CHARGE_WINDOW_MIN_HOURS
    assert res["duration_hours"] >= energy_defaults.CHARGE_WINDOW_MIN_HOURS


def test_real_shape_2026_10_22_fixture():
    data = load_fixture("sample_hourly_2026_10_22.json")
    frames = data["frames"]
    prices = [f["metrics"]["pricing"]["price_gross"] for f in frames]
    mean = sum(prices) / len(prices)
    prof = pstryk_engine.analyse_day_price_profile(frames)
    segs = _segments(prof)

    # every above-mean hour is covered by a peak, every below-mean hour by a valley
    for f, price in zip(frames, prices):
        m = _warsaw_m(f["start"])
        seg = [s for s in segs if s["start_m"] <= m < s["end_m"]][0]
        assert seg["kind"] == ("peak" if price >= mean else "valley")

    pb = pstryk_engine.find_powerbank_best_window(data, target_hours="auto", **ENERGY_KW)
    start, end = _win_minutes(pb)
    valley = _valley_containing(frames, start)
    cheapest = min(range(24), key=lambda h: prices[h])
    assert valley["start_m"] <= cheapest * 60 < valley["end_m"]          # window sits in the cheapest valley
    assert valley["start_m"] <= start and end <= valley["end_m"]
    total_h = (end - start) // 60
    assert total_h >= _min_hours()
    # stretch rule: hours beyond the cheapest N-block are all <= the valley mean; each valley neighbour left out
    # is above the valley mean or would push the current below the floor
    window_h = list(range(start // 60, end // 60))
    n = _min_hours()
    base = min(range(len(window_h) - n + 1), key=lambda i: (sum(prices[h] for h in window_h[i:i + n]), i))
    extra = window_h[:base] + window_h[base + n:]
    assert all(prices[h] <= valley["avg"] for h in extra)
    for h in (start // 60 - 1, end // 60):
        if valley["start_m"] <= h * 60 < valley["end_m"]:
            assert prices[h] > valley["avg"] or _expected_current(n, total_h + 1) < _FLOOR_A

    dw = pstryk_engine.find_best_discharge_windows(data)
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=ENERGY_KW["current_soc"], max_soc=ENERGY_KW["max_soc"], min_soc=20,
        best_pb_window=pb, best_discharge_windows=dw, pricing_data=data, max_charge_current=ENERGY_KW["ceiling_a"],
        max_discharge_current=140.0, battery_capacity_kwh=ENERGY_KW["capacity_kwh"],
        nominal_voltage=ENERGY_KW["voltage"], loss_buffer_pct=ENERGY_KW["loss_pct"])
    assert_valid_schedule(slots)
    charge = [s for s in slots if s["grid_charge"]]
    assert len(charge) == 1 and slot_minutes(charge)[0] == start
    assert charge[0]["target_charge_current_a"] == _expected_current(_min_hours(), total_h)
    # discharge peaks (Rule 12: ranked by retail gross price) start at the segment peak ranking, never before the charge ended
    peak_slots = [s for s in slots if not s["grid_charge"] and s["target_soc"] == 20]
    assert peak_slots


def test_flat_price_day_gives_valid_schedule_without_division_by_zero():
    frames = _curve_frames([1.0] * 24)
    _, _, slots = _tou_for(frames)
    assert_valid_schedule(slots)


def test_generate_without_pricing_data_returns_none():
    assert pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(3, 6)) is None
    assert pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20) is None


def test_sync_does_not_write_when_schedule_is_none(tmp_path, monkeypatch):
    cache_dir = str(tmp_path)
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), {
        "_warsaw_date": "2026-10-17", "powerbank_best_window": win(3, 6), "best_sell_window": win(17, 20),
    })
    freeze_now(monkeypatch, datetime(2026, 10, 17, 10, 0, tzinfo=WARSAW))
    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {}}
    client.sync_tou_schedule.return_value = {"success": True}
    with mock.patch("pstryk_engine.solarman_v5_client", client), \
         mock.patch("pstryk_engine.generate_deye_tou_schedule", return_value=None):
        res = pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir, force=True)
    assert res["success"] is False
    client.sync_tou_schedule.assert_not_called()


def test_sync_passes_pricing_data_and_discharge_windows_to_generator(tmp_path, monkeypatch):
    cache_dir = str(tmp_path)
    frames = warsaw_day_frames(datetime(2026, 10, 17).date(), lambda h: BASE_CURVE[h])
    dw = pstryk_engine.find_best_discharge_windows({"frames": frames})
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), {
        "_warsaw_date": "2026-10-17", "powerbank_best_window": win(3, 6), "best_sell_window": win(17, 20),
        "best_discharge_windows": dw, "frames": frames,
    })
    freeze_now(monkeypatch, datetime(2026, 10, 17, 10, 0, tzinfo=WARSAW))
    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {}}
    client.sync_tou_schedule.return_value = {"success": True}
    real = pstryk_engine.generate_deye_tou_schedule
    with mock.patch("pstryk_engine.solarman_v5_client", client), \
         mock.patch("pstryk_engine.generate_deye_tou_schedule", side_effect=real) as gen:
        pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir, force=True)
    kwargs = gen.call_args.kwargs
    assert kwargs.get("pricing_data") and kwargs["pricing_data"].get("frames")
    assert kwargs.get("best_discharge_windows") and kwargs["best_discharge_windows"].get("peaks")


def test_consolidated_data_stores_discharge_windows_for_cache_hits(monkeypatch, tmp_path):
    freeze_now(monkeypatch, datetime(2026, 10, 24, 0, 30, tzinfo=WARSAW))
    sat = datetime(2026, 10, 24).date()
    frames = warsaw_day_frames(sat, lambda h: BASE_CURVE[h]) + warsaw_day_frames(sat + timedelta(days=1), lambda h: BASE_CURVE[h])
    with mock.patch("pstryk_engine.fetch_api", side_effect=_consolidated_mock(frames)):
        res = pstryk_engine.fetch_consolidated_data(api_key="sk-T", use_cache=True, cache_dir=str(tmp_path))
    assert res["best_discharge_windows"].get("peaks")
    assert "best_discharge_windows_tomorrow" in res
    cached = pstryk_engine.read_cache(pstryk_engine.get_cache_file_path("dol", str(tmp_path)))
    assert cached["best_discharge_windows"] == res["best_discharge_windows"]


def test_reg108_target_clamped_to_configured_ceiling_not_literal_100(monkeypatch):
    monkeypatch.setattr(energy_defaults, "BATTERY_CHARGE_CURRENT_MAX_CEILING_A", 80.0)
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(3, 6), best_sell_window=win(17, 20))
    assert _modulate(monkeypatch, slots, 12, 0, next_cycle=95) == 80      # next cycle clamped to ceiling
    assert _modulate(monkeypatch, slots, 12, 0, next_cycle=None) == 80    # no preset -> ceiling baseline
    assert _modulate(monkeypatch, slots, 23, 30, next_cycle=60) == 60     # after today's window: preset, no 19:00 gate


# ---- Peak top-up planner (SOC projection from now to the next charge; minimal grid top-up for uncovered peaks) ----
TOPUP_DAY = datetime(2026, 10, 22).date()
TOPUP_NEXT = TOPUP_DAY + timedelta(days=1)
# today: evening peak 17-21, cheap 23:00; tomorrow: morning peak 06-09, main charge valley 13-14, evening peak 15-20
TOPUP_TODAY = [1.0] * 6 + [2.0] * 4 + [1.0] * 7 + [2.0] * 5 + [0.95, 0.80]
TOPUP_TOMORROW = [0.90, 0.85] + [0.90] * 4 + [2.0] * 4 + [1.2] * 3 + [0.7, 0.7] + [2.0] * 6 + [1.0] * 3
TOPUP_KW = dict(min_soc=20, max_soc=90, capacity_kwh=10.0, voltage=50.0, ceiling_a=100.0, loss_pct=0.0, load_kw=1.0)


def _topup_payload():
    today = _curve_frames(TOPUP_TODAY, TOPUP_DAY)
    tomorrow = _curve_frames(TOPUP_TOMORROW, TOPUP_NEXT)
    main = {"start": tomorrow[13]["start"], "end": tomorrow[14]["end"]}
    return {"pricing_frames_today": today, "pricing_frames_tomorrow": tomorrow,
            "powerbank_best_window": {"start": today[0]["start"], "end": today[1]["end"]},
            "powerbank_best_window_tomorrow": main}


def _at(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=WARSAW)


def _plan(now, soc):
    p = _topup_payload()
    return pstryk_engine.plan_peak_topups(
        [p["pricing_frames_today"], p["pricing_frames_tomorrow"]],
        [p["powerbank_best_window"], p["powerbank_best_window_tomorrow"]],
        now_dt=now, current_soc=soc, **TOPUP_KW)


def test_topup_placed_at_cheapest_hour_before_uncovered_morning_peak_sized_to_deficit():
    tops = _plan(_at(TOPUP_DAY, 22), soc=20)
    assert len(tops) == 1  # the tomorrow evening peak follows the main charge -> covered
    t = tops[0]
    # deficit 4 h x 1 kW = 4 kWh at 5 kW -> 48 min (-> 50 at the 5-min step), cheapest hour before the peak: 23:00
    assert t["start"] == _at(TOPUP_DAY, 23)
    assert t["end"] == _at(TOPUP_DAY, 23, 50)
    assert t["target_soc"] == 60
    assert t["charge_current_a"] == 100


def test_no_topup_when_battery_covers_the_peak():
    # tomorrow's prices are known: the 22:00 -> 06:00 valley drains only to the peak's need (20 + 4 kWh / 10 kWh = 60 %)
    assert _plan(_at(TOPUP_DAY, 22), soc=80) == []
    assert _plan(_at(TOPUP_DAY, 22), soc=60) == []
    assert _plan(_at(TOPUP_DAY, 22), soc=50)  # below the need nothing is drained, the missing energy is topped up


def test_topup_never_starts_in_the_past_and_needs_enough_time():
    tops = _plan(_at(TOPUP_DAY, 23, 30), soc=20)
    assert len(tops) == 1
    # 23:30-24:00 is too short for 48 min, so the next cheapest full hour (01:00, 0.85) wins
    assert tops[0]["start"] == _at(TOPUP_NEXT, 1)
    assert tops[0]["start"] >= _at(TOPUP_DAY, 23, 30)


def test_topup_is_capped_by_max_soc():
    big = dict(TOPUP_KW, load_kw=5.0)
    p = _topup_payload()
    tops = pstryk_engine.plan_peak_topups(
        [p["pricing_frames_today"], p["pricing_frames_tomorrow"]],
        [p["powerbank_best_window"], p["powerbank_best_window_tomorrow"]],
        now_dt=_at(TOPUP_DAY, 22), current_soc=20, **big)
    assert tops[0]["target_soc"] == 90


def test_cross_midnight_topup_lands_in_today_table_with_ceiling_current_and_six_slots():
    p = _topup_payload()
    now = _at(TOPUP_DAY, 22)
    kw = dict(current_soc=20, min_soc=20, max_soc=90, battery_capacity_kwh=10.0, nominal_voltage=50.0,
              loss_buffer_pct=0.0, max_discharge_current=140.0)
    today_tops = pstryk_engine.peak_topups_for_day(p, False, now, current_soc=20, min_soc=20, max_soc=90,
                                                   capacity_kwh=10.0, voltage=50.0, loss_pct=0.0, load_kw=1.0)
    slots = pstryk_engine.generate_deye_tou_schedule(
        best_pb_window=p["powerbank_best_window"], pricing_data={"frames": p["pricing_frames_today"]},
        topup_windows=today_tops, **kw)
    assert_valid_schedule(slots)
    top = [s for s in slots if s["grid_charge"] and s["time"] == "23:00"]
    assert top and top[0]["target_soc"] == 60
    assert top[0]["target_charge_current_a"] == int(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A)
    # the slot after the top-up ends at 23:50 (step-rounded) and holds the topped-up level
    nxt = slots[(slots.index(top[0]) + 1) % 6]
    assert nxt["time"] == "23:50" and not nxt["grid_charge"]
    # tomorrow's table carries nothing from today's top-up
    assert pstryk_engine.peak_topups_for_day(p, True, now, current_soc=20, min_soc=20, max_soc=90,
                                             capacity_kwh=10.0, voltage=50.0, loss_pct=0.0, load_kw=1.0) == []


def test_topup_after_midnight_is_clipped_into_tomorrow_table():
    p = _topup_payload()
    tops = pstryk_engine.peak_topups_for_day(p, True, _at(TOPUP_DAY, 23, 30), current_soc=20, min_soc=20,
                                             max_soc=90, capacity_kwh=10.0, voltage=50.0, loss_pct=0.0, load_kw=1.0)
    assert len(tops) == 1 and tops[0]["start"] == _at(TOPUP_NEXT, 1)


def test_reg108_modulation_covers_topup_window(monkeypatch):
    calls = []
    monkeypatch.setattr(pstryk_engine, "set_charge_current_with_fallback", lambda h, p, a: calls.append(a) or {"success": True})
    freeze_now(monkeypatch, _at(TOPUP_DAY, 23, 10))
    slots = [{"time": t, "grid_charge": g, "target_charge_current_a": a} for t, g, a in [
        ("00:00", False, 50), ("06:00", False, 50), ("10:00", False, 50), ("13:00", True, 50),
        ("15:00", False, 50), ("23:00", True, 100)]]
    res = pstryk_engine.check_and_apply_charge_current_modulation(slots)
    assert res["in_charging_window"] and calls == [100]


def test_estimate_household_load_from_daily_consumption():
    assert pstryk_engine.estimate_household_load_kw({"daily_consumption": 12.0}, _at(TOPUP_DAY, 12)) == 1.0
    assert pstryk_engine.estimate_household_load_kw({}, _at(TOPUP_DAY, 12)) is None
    assert pstryk_engine.estimate_household_load_kw({"daily_consumption": 5.0}, _at(TOPUP_DAY, 0)) is None


def test_topup_never_overlaps_an_existing_charge_window():
    # tomorrow's evening peak is under-covered by the 2 h main charge at 5 kW load; the only gap hours before it are
    # the main charge itself, so no top-up may be stacked on it
    p = _topup_payload()
    tops = pstryk_engine.plan_peak_topups(
        [p["pricing_frames_today"], p["pricing_frames_tomorrow"]],
        [p["powerbank_best_window"], p["powerbank_best_window_tomorrow"]],
        now_dt=_at(TOPUP_NEXT, 10), current_soc=20, **dict(TOPUP_KW, load_kw=5.0))
    main_a = _at(TOPUP_NEXT, 13)
    main_b = _at(TOPUP_NEXT, 15)
    assert all(t["end"] <= main_a or t["start"] >= main_b for t in tops)
    assert all(t["start"] >= _at(TOPUP_NEXT, 10) for t in tops)


# ---- Peak top-up review fixes: fresh load estimate, no-data, economic guard, contiguous fallback, step, flag ----
def _cache(monkeypatch, data):
    import solarman_v5_client
    seen = {}

    def fake(ignore_ttl=False, **kw):
        seen["ignore_ttl"] = ignore_ttl
        return None if data is None else data
    monkeypatch.setattr(solarman_v5_client, "load_deye_cache", fake)
    return seen


def test_household_load_ignores_yesterday_cache_at_0005(monkeypatch):
    now = _at(TOPUP_DAY, 0, 5)
    yesterday = (now - timedelta(minutes=30)).timestamp()
    seen = _cache(monkeypatch, {"cache_updated_at": yesterday, "telemetry": {"daily_consumption": 20.0, "consumption_power": 900}})
    assert pstryk_engine.household_load_kw(None, now, 7.0) is None
    assert seen["ignore_ttl"] is False


def test_household_load_first_hour_uses_instantaneous_power(monkeypatch):
    now = _at(TOPUP_DAY, 0, 5)
    # daily_consumption still carries yesterday's 20 kWh (inverter reset lag) -> must not be divided by 5 min
    assert pstryk_engine.household_load_kw({"daily_consumption": 20.0, "consumption_power": 800}, now, 7.0) == 0.8
    assert pstryk_engine.household_load_kw({"daily_consumption": 20.0}, now, 7.0) is None


def test_household_load_no_telemetry_is_none_not_rated_power(monkeypatch):
    _cache(monkeypatch, None)
    assert pstryk_engine.household_load_kw(None, _at(TOPUP_DAY, 12), 7.0) is None
    p = _topup_payload()
    assert pstryk_engine.peak_topups_for_day(p, False, _at(TOPUP_DAY, 22), current_soc=20, min_soc=20, max_soc=90,
                                             capacity_kwh=10.0, voltage=50.0, loss_pct=0.0, load_kw=None) == []


def test_household_load_clamped_to_rated_discharge():
    assert pstryk_engine.household_load_kw({"daily_consumption": 100.0}, _at(TOPUP_DAY, 2), 7.0) == 7.0


def test_household_load_dst_elapsed_hours_via_utc():
    # 2026-10-25 fall-back: 03:00 CET is 4 real hours after local midnight (00:00 CEST)
    now = datetime(2026, 10, 25, 3, 0, tzinfo=WARSAW)
    assert pstryk_engine.household_load_kw({"daily_consumption": 8.0}, now, 7.0) == 2.0


def test_no_topup_when_valley_not_cheaper_than_peak_after_losses():
    p = _topup_payload()
    tops = pstryk_engine.plan_peak_topups(
        [p["pricing_frames_today"], p["pricing_frames_tomorrow"]],
        [p["powerbank_best_window"], p["powerbank_best_window_tomorrow"]],
        now_dt=_at(TOPUP_DAY, 22), current_soc=20, **dict(TOPUP_KW, loss_pct=200.0))
    assert tops == []  # 0.80 x 3 = 2.4 >= peak 2.0


def test_topup_fallback_uses_cheapest_contiguous_run_not_spanning_charges():
    prices = [0.5, 0.4, 0.3, 0.3, 0.4, 0.5] + [2.0] * 4 + [0.9] * 14
    frames = _curve_frames(prices, TOPUP_DAY)
    charges = [{"start": frames[1]["start"], "end": frames[1]["end"]},
               {"start": frames[4]["start"], "end": frames[4]["end"]}]
    tops = pstryk_engine.plan_peak_topups(
        [frames], charges, now_dt=_at(TOPUP_DAY, 0), current_soc=20, min_soc=20, max_soc=90,
        capacity_kwh=100.0, voltage=50.0, ceiling_a=100.0, loss_pct=0.0, load_kw=8.0)
    assert len(tops) == 1
    assert tops[0]["start"] == _at(TOPUP_DAY, 2) and tops[0]["end"] == _at(TOPUP_DAY, 4)


def test_topup_start_and_duration_snap_to_minute_step(monkeypatch):
    monkeypatch.setattr(energy_defaults, "TOU_SLOT_MINUTE_STEP", 5)
    p = _topup_payload()
    tops = pstryk_engine.plan_peak_topups(
        [p["pricing_frames_today"], p["pricing_frames_tomorrow"]],
        [p["powerbank_best_window"], p["powerbank_best_window_tomorrow"]],
        now_dt=_at(TOPUP_DAY, 23, 2), current_soc=20, **dict(TOPUP_KW, load_kw=1.03))
    # now inside the 23:00 hour -> the slot keeps its hour-boundary start (no drift with the clock); it charges from
    # ceil(23:02) -> 23:05 for 50 min (load 1.03 snaps to 1.0 kW)
    assert tops[0]["start"] == _at(TOPUP_DAY, 23)
    assert tops[0]["end"] == _at(TOPUP_DAY, 23, 55)


def test_topup_slot_flagged_and_badge_picks_main_charge():
    import jinja2
    import yaml
    p = _topup_payload()
    now = _at(TOPUP_DAY, 22)
    tops = pstryk_engine.peak_topups_for_day(p, False, now, current_soc=20, min_soc=20, max_soc=90,
                                             capacity_kwh=10.0, voltage=50.0, loss_pct=0.0, load_kw=1.0)
    slots = pstryk_engine.generate_deye_tou_schedule(
        current_soc=20, min_soc=20, max_soc=90, battery_capacity_kwh=10.0, nominal_voltage=50.0, loss_buffer_pct=0.0,
        max_discharge_current=140.0, best_pb_window=p["powerbank_best_window"],
        pricing_data={"frames": p["pricing_frames_today"]}, topup_windows=tops)
    flagged = [s for s in slots if s.get("topup")]
    assert len(flagged) == 1 and flagged[0]["time"] == "23:00"
    assert all(s["topup"] is False for s in slots if s is not flagged[0])
    # badge template (templates.yaml) selects the main charge slot, not the top-up
    for s in slots:
        if s.get("topup"):
            s["target_charge_current_a"] = 999
    path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "templates.yaml")
    src = open(path, encoding="utf-8").read()
    i = src.index("unique_id: deye_inverter_recommended_charge_current")
    body = src[i:src.index("icon:", i)]
    state_tpl = body.split("state: >", 1)[1].split("attributes:", 1)[0]
    env = jinja2.Environment()
    ctx = dict(state_attr=lambda e, a: slots if a == "deye_tou_schedule_today" else None,
               states=lambda e: "100")
    env.filters["int"] = lambda v, d=0: int(float(v)) if str(v).replace(".", "").isdigit() else d
    out = env.from_string(state_tpl).render(**ctx).strip()
    assert out != "999" and int(out) <= 100


# =====================================================================================================================
# TDD (plan try-to-continue-this-zippy-cloud): data-driven reserve, floor == projected SOC holds, shared SOC projection
# ASSUMED API (implementer must match; tests call exactly this):
#   project_soc_timeline(slots, start_soc, load_kw, now_dt, *, min_soc, max_soc, capacity_kwh, charge_kw) -> list[dict]
#     slots:   [{"start": aware dt, "end": aware dt, "target_soc": int|float, "grid_charge": bool}, ...] time ordered
#     returns one dict per slot (clipped to >= now_dt, slots fully in the past dropped):
#              {"start", "end", "soc_start": float, "soc_end": float}   (SOC in %)
#     Deye semantics: non-charge slot: battery discharges for load_kw down to target_soc (never below it; if soc is
#              already <= target_soc it stays - the grid covers the load, no grid charge); charge slot: grid charges
#              at charge_kw (kW into the battery, loss already applied) up to target_soc, never above max_soc.
#   generate_deye_tou_schedule(..., load_kw: Optional[float] = None, now_dt: Optional[datetime] = None)
#     load_kw None/0 -> no reserve; now_dt = instant at which current_soc is valid (default: now).
#     A reserve is data-driven only (no fixed pre-peak margin).
# =====================================================================================================================
TWO_DAY_FIXTURE = "two_day_2026_10_22_23.json"
D22, D23 = datetime(2026, 10, 22).date(), datetime(2026, 10, 23).date()
CAP_KWH, VOLT, LOSS = 16.13, 51.2, 10.0
CHARGE_KW = 100 * VOLT / 1000 / (1 + LOSS / 100)


def _dt_at(day, h, m=0):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=WARSAW)


def _slots_to_segments(slots, day):
    """Table slots (HH:MM, wrapping) -> aware segments covering the Warsaw day."""
    ms = slot_minutes(slots)
    base = datetime(day.year, day.month, day.day, tzinfo=WARSAW)
    segs = []
    for i, s in enumerate(slots):
        a, b = ms[i], (ms[i + 1] if i + 1 < len(slots) else ms[0] + 1440)
        segs.append({"start": base + timedelta(minutes=a), "end": base + timedelta(minutes=b),
                     "target_soc": s["target_soc"], "grid_charge": s["grid_charge"], "slot": s})
    # the wrapping last slot also covers [00:00, first start)
    if ms[0] > 0:
        segs.insert(0, dict(segs[-1], start=base, end=base + timedelta(minutes=ms[0])))
        segs[-1] = dict(segs[-1], end=base + timedelta(days=1))
    return segs


def _gen(prices_by_hour, *, soc=60, load=None, now=None, day=D23, pb=None, **kw):
    params = dict(current_soc=soc, min_soc=20, max_soc=90, battery_capacity_kwh=CAP_KWH, nominal_voltage=VOLT,
                  loss_buffer_pct=LOSS, max_discharge_current=140.0, best_pb_window=pb,
                  pricing_data={"frames": _curve_frames(prices_by_hour, day)})
    if load is not None:
        params["load_kw"] = load
    params["now_dt"] = now or _dt_at(day, 0)
    params.update(kw)
    return pstryk_engine.generate_deye_tou_schedule(**params)


def _soc_projection(slots, day, soc, load, now):
    return pstryk_engine.project_soc_timeline(
        _slots_to_segments(slots, day), soc, load, now, min_soc=20, max_soc=90, capacity_kwh=CAP_KWH, charge_kw=CHARGE_KW)


# ---- project_soc_timeline (pure) ----
def test_project_soc_timeline_semantics():
    segs = [
        {"start": _dt_at(D23, 0), "end": _dt_at(D23, 2), "target_soc": 50, "grid_charge": False},   # hold at 50
        {"start": _dt_at(D23, 2), "end": _dt_at(D23, 4), "target_soc": 20, "grid_charge": False},   # drain 1 kW
        {"start": _dt_at(D23, 4), "end": _dt_at(D23, 5), "target_soc": 90, "grid_charge": True},    # charge
        {"start": _dt_at(D23, 5), "end": _dt_at(D23, 6), "target_soc": 60, "grid_charge": False},   # floor under soc
    ]
    kw = dict(min_soc=20, max_soc=90, capacity_kwh=10.0, charge_kw=5.0)
    out = pstryk_engine.project_soc_timeline(segs, 50, 1.0, _dt_at(D23, 0), **kw)
    assert [round(o["soc_start"], 1) for o in out] == [50.0, 50.0, 30.0, 80.0]
    assert round(out[1]["soc_end"], 1) == 30.0 and round(out[2]["soc_end"], 1) == 80.0
    assert round(out[3]["soc_end"], 1) == 70.0
    # battery below the floor: no drain, no grid charge
    low = pstryk_engine.project_soc_timeline(segs[:1], 30, 1.0, _dt_at(D23, 0), **kw)
    assert low[0]["soc_start"] == 30 and low[0]["soc_end"] == 30
    # now_dt clips: a slot fully in the past is dropped, a running one starts at now
    cut = pstryk_engine.project_soc_timeline(segs, 50, 1.0, _dt_at(D23, 3), **kw)
    assert cut[0]["start"] == _dt_at(D23, 3) and round(cut[0]["soc_start"], 1) == 50.0 and len(cut) == 3


# ---- hold slots ----
def _hold_roles(slots):
    return [s for s in slots if not s["grid_charge"] and s["role"] in ("reserve", "other")]


@pytest.mark.parametrize("load", [0.5, 1.0])
@pytest.mark.parametrize("soc", [25, 55, 85])
def test_hold_slots_never_drain_or_grid_charge_and_floor_never_above_projected_soc(soc, load):
    fx = load_fixture(TWO_DAY_FIXTURE)
    pr = [pstryk_engine.retail_gross_price(f) for f in fx["pricing_frames_tomorrow"]]
    slots = _gen(pr, soc=soc, load=load, now=_dt_at(D23, 0), pb=fx["powerbank_best_window_tomorrow"])
    proj = _soc_projection(slots, D23, soc, load, _dt_at(D23, 0))
    for seg, p in zip([s for s in _slots_to_segments(slots, D23) if s["end"] > _dt_at(D23, 0)], proj):
        s = seg["slot"]
        if s["grid_charge"]:
            continue
        # no floor above the projected level (else the house would run on the grid)
        assert s["target_soc"] <= int(p["soc_start"] + 1e-6) or s["target_soc"] == 20, (s, p)
        if s["role"] == "reserve":  # a valley hold: min SOC .. projected SOC (drain-to-need), never above projected
            assert 20 <= s["target_soc"] <= int(p["soc_start"] + 1e-6), (s, p)


def test_midnight_slot_is_projected_soc_or_min_soc_never_copied_evening_reserve():
    fx = load_fixture(TWO_DAY_FIXTURE)
    pr = [pstryk_engine.retail_gross_price(f) for f in fx["pricing_frames_tomorrow"]]
    slots = _gen(pr, soc=55, load=0.5, now=_dt_at(D23, 0), pb=fx["powerbank_best_window_tomorrow"])
    mid = _slots_to_segments(slots, D23)[0]["slot"]  # the slot covering 00:00
    assert mid["grid_charge"] is False
    # projected (55), min (20) or drain-to-next-peak-need in between; never above the projected SOC
    assert 20 <= mid["target_soc"] <= 55, mid


def test_midnight_slot_drains_to_morning_peak_need_on_two_day_fixture():
    fx = load_fixture(TWO_DAY_FIXTURE)
    pr = [pstryk_engine.retail_gross_price(f) for f in fx["pricing_frames_tomorrow"]]
    pb = fx["powerbank_best_window_tomorrow"]
    slots = _gen(pr, soc=70, load=0.5, now=_dt_at(D23, 0), pb=pb)
    mid = _slots_to_segments(slots, D23)[0]
    peak = next(s for s in slots if s["role"] == "peak")
    hrs = sum((g["end"] - g["start"]).total_seconds() / 3600.0 for g in _slots_to_segments(slots, D23)
              if g["slot"]["role"] == "peak" and g["start"] < _dt_at(D23, 12))
    # next event after 00:00 is the morning peak (a charge refills only later)
    assert slots[0]["role"] == "reserve" or mid["slot"]["role"] == "reserve", slots
    exp = 20 + hrs * 0.5 / CAP_KWH * 100  # snapped up to the TOU_FLOOR_SOC_STEP grid
    assert exp - 1e-6 <= mid["target_soc"] < exp + energy_defaults.TOU_FLOOR_SOC_STEP, (mid["target_soc"], exp, peak)
    assert mid["target_soc"] < 70


def test_after_last_peak_slot_is_min_soc_not_projected():
    fx = load_fixture(TWO_DAY_FIXTURE)
    pr = [pstryk_engine.retail_gross_price(f) for f in fx["pricing_frames_tomorrow"]]
    slots = _gen(pr, soc=60, load=0.5, now=_dt_at(D23, 0, 30), pb=fx["powerbank_best_window_tomorrow"])
    segs = _slots_to_segments(slots, D23)
    last_peak_end = max(g["end"] for g in segs if g["slot"]["role"] == "peak")
    for g in segs:
        if g["start"] >= last_peak_end and not g["grid_charge"]:
            assert g["target_soc"] == 20, g


# ---- data-driven reserve (no charge window, so only the peak-vs-peak decision exists) ----
def _two_peaks(morning, evening):
    return [0.5] * 6 + [morning] * 3 + [0.6] * 8 + [evening] * 3 + [0.5] * 4


def _peak_slot(slots, hour):
    return next(s for s in slots if s["role"] == "peak" and int(s["time"][:2]) <= hour < int(s["time"][:2]) + 3)


def test_no_reserve_wrapping_to_same_days_morning():
    # evening peak is the cheaper one; the pricier morning is the SAME day's (already past) -> no reserve, even with load
    slots = _gen(_two_peaks(2.0, 1.0), soc=70, load=1.0)
    assert _peak_slot(slots, 17)["target_soc"] == 20


def test_reserve_only_when_later_peak_pricier_by_more_than_loss():
    # loss 10%: 1.05 vs 1.0 is within the loss -> none; 2.0 vs 1.0 -> reserve
    assert _peak_slot(_gen(_two_peaks(1.0, 1.05), soc=70, load=1.0), 6)["target_soc"] == 20
    assert _peak_slot(_gen(_two_peaks(1.0, 2.0), soc=70, load=1.0), 6)["target_soc"] > 20


def test_reserve_sized_by_load_times_later_peak_hours_over_capacity():
    expect = lambda load: 20 + 3 * load / CAP_KWH * 100  # later peak = 3 h
    for load in (0.5, 1.0):
        got = _peak_slot(_gen(_two_peaks(1.0, 2.0), soc=80, load=load), 6)["target_soc"]
        assert abs(got - expect(load)) <= 1.5, (load, got)
    assert _peak_slot(_gen(_two_peaks(1.0, 2.0), soc=80, load=50.0), 6)["target_soc"] <= 90  # capped at max SOC


def test_no_reserve_without_load_estimate():
    assert _peak_slot(_gen(_two_peaks(1.0, 2.0), soc=80), 6)["target_soc"] == 20
    assert _peak_slot(_gen(_two_peaks(1.0, 2.0), soc=80, load=0.0), 6)["target_soc"] == 20


# ---- top-up on the real 22 -> 23.10 price shape ----
TOPUP_REAL = dict(min_soc=20, max_soc=90, capacity_kwh=CAP_KWH, voltage=VOLT, loss_pct=LOSS)


@pytest.mark.parametrize("load", [0.5, 1.0])
def test_topup_lands_at_today_2300_not_after_midnight(load):
    fx = load_fixture(TWO_DAY_FIXTURE)
    tops = pstryk_engine.peak_topups_for_day(fx, False, _dt_at(D22, 22), current_soc=30, load_kw=load, **TOPUP_REAL)
    assert tops and tops[0]["start"] == _dt_at(D22, 23), tops
    assert all(t["end"] <= _dt_at(D23, 0) for t in tops)
    # sized to the morning peak (3+ h at load) from the shared projection: target covers min SOC + peak energy
    assert tops[0]["target_soc"] >= 20 + 3 * load / CAP_KWH * 100 - 1


# ---- energy balance (port of scratchpad/sim.py): zero grid purchase in peaks when battery + top-ups can cover ----
def _price_map(fx):
    m = {}
    for k in ("pricing_frames_today", "pricing_frames_tomorrow"):
        for f in pstryk_engine.priced_day_frames(fx[k]):
            m[pstryk_engine._iso_dt(f["start"]).astimezone(WARSAW).replace(tzinfo=None)] = pstryk_engine.retail_gross_price(f)
    return m


def _peak_hours(fx):
    hrs = set()
    for k in ("pricing_frames_today", "pricing_frames_tomorrow"):
        priced = pstryk_engine.priced_day_frames(fx[k])
        for sg in pstryk_engine.analyse_day_price_profile(priced)["segments"]:
            if sg["kind"] == "peak":
                for f in priced[sg["start_index"]:sg["end_index"]]:
                    hrs.add(pstryk_engine._iso_dt(f["start"]).astimezone(WARSAW).replace(tzinfo=None))
    return hrs


def _sim_table(now, payload, soc, load):
    pr = {"frames": payload["pricing_frames_today"]}
    pb = pstryk_engine.find_powerbank_best_window(pr, "auto", now_dt=now, current_soc=soc, max_soc=90,
                                                  capacity_kwh=CAP_KWH, voltage=VOLT, loss_pct=LOSS)
    payload = dict(payload, powerbank_best_window=payload.get("powerbank_best_window") or pb)
    tops = pstryk_engine.peak_topups_for_day(payload, False, now, current_soc=soc, load_kw=load, **TOPUP_REAL)
    return pstryk_engine.generate_deye_tou_schedule(next_day=pstryk_engine.next_day_outlook(payload, False),
        current_soc=soc, max_soc=90, min_soc=20, best_pb_window=pb, battery_capacity_kwh=CAP_KWH, nominal_voltage=VOLT,
        loss_buffer_pct=LOSS, best_discharge_windows=pstryk_engine.find_best_discharge_windows(pr), pricing_data=pr,
        topup_windows=tops, max_discharge_current=140, load_kw=load, now_dt=now)


def _run_balance(load, hold_reserve=False, now=None, soc0=60, trace=None):
    fx = load_fixture(TWO_DAY_FIXTURE)
    price, peaks = _price_map(fx), _peak_hours(fx)
    now = now or _dt_at(D22, 17, 45)
    payload = {"pricing_frames_today": fx["pricing_frames_today"], "pricing_frames_tomorrow": fx["pricing_frames_tomorrow"],
               "powerbank_best_window": fx["powerbank_best_window"],
               "powerbank_best_window_tomorrow": fx["powerbank_best_window_tomorrow"]}
    sl, soc, t, dt, resynced, grid_peak, grid_valley = _sim_table(now, payload, soc0, load), float(soc0), now, 0.25, False, 0.0, 0.0
    end = _dt_at(D23, 11, 45)
    cur = lambda sl, t: next((s for s in reversed(sl) if int(s["time"][:2]) * 60 + int(s["time"][3:]) <= t.hour * 60 + t.minute), sl[-1])
    while t < end:
        if not resynced and t >= _dt_at(D23, 0, 30):
            p2 = {"pricing_frames_today": fx["pricing_frames_tomorrow"], "pricing_frames_tomorrow": None, "powerbank_best_window": None}
            sl, resynced = _sim_table(t, p2, int(round(soc)), load), True
        s, e, g = cur(sl, t), load * dt, 0.0
        if s["grid_charge"] and soc < s["target_soc"]:
            add = min(CHARGE_KW * dt, (s["target_soc"] - soc) / 100 * CAP_KWH)
            soc += add / CAP_KWH * 100
            g = e + add * 1.1
            if trace is not None and add > 1e-9:
                trace.append(t)
        elif soc > s["target_soc"] and not (hold_reserve and s["role"] == "reserve"):
            take = min(e, (soc - s["target_soc"]) / 100 * CAP_KWH)
            soc -= take / CAP_KWH * 100
            g = e - take
        else:
            g = e
        if t.replace(minute=0, tzinfo=None) in peaks and not s["grid_charge"]:
            grid_peak += g
        elif not s["grid_charge"]:
            grid_valley += g
        t += timedelta(minutes=15)
    return grid_peak, grid_valley


@pytest.mark.parametrize("load", [0.5, 1.0])
def test_energy_balance_no_grid_purchase_in_peaks_when_coverable(load):
    grid_peak, grid_valley = _run_balance(load)
    assert grid_peak < 0.05, f"{grid_peak:.2f} kWh bought from the grid during peak hours at {load} kW"
    # draining valleys to the next peak's need must not buy more in valleys than holding the level (and at
    # 0.5 kW strictly less)
    _, hold_valley = _run_balance(load, hold_reserve=True)
    assert grid_valley <= hold_valley + 1e-9, (grid_valley, hold_valley)
    if load == 0.5:
        assert grid_valley < hold_valley, (grid_valley, hold_valley)


# ---- regression: tomorrow's prices known -> today's table and the planner look into tomorrow ----
@pytest.mark.parametrize("now,soc0,load", [((17, 45), 60, 1.0), ((22, 1), 30, 1.0), ((22, 1), 30, 0.5)])
def test_full_resync_flow_tops_up_at_today_2300_never_after_midnight(now, soc0, load):
    trace = []
    _run_balance(load, now=_dt_at(D22, *now), soc0=soc0, trace=trace)
    assert trace, "a top-up was expected"
    assert all(_dt_at(D22, 23) <= t < _dt_at(D23, 0) for t in trace), [t.strftime("%d %H:%M") for t in trace]


@pytest.mark.parametrize("load", [0.5, 1.0])
def test_todays_evening_valley_floor_keeps_energy_tomorrows_morning_peak_needs(load):
    fx = load_fixture(TWO_DAY_FIXTURE)
    payload = {k: fx[k] for k in ("pricing_frames_today", "pricing_frames_tomorrow", "powerbank_best_window",
                                  "powerbank_best_window_tomorrow")}
    sl = _sim_table(_dt_at(D22, 22, 1), payload, 60, load)
    evening = next(s for s in sl if s["time"] == "22:00")
    assert evening["target_soc"] > 20 and not evening["grid_charge"], evening
    # tomorrow unknown -> the old per-day cut: drained to Min SOC
    only_today = dict(payload, pricing_frames_tomorrow=None)
    assert next(s for s in _sim_table(_dt_at(D22, 22, 1), only_today, 60, load) if s["time"] == "22:00")["target_soc"] == 20


def test_topup_slots_are_not_shorter_than_three_minute_steps(monkeypatch):
    monkeypatch.setattr(energy_defaults, "TOU_SLOT_MINUTE_STEP", 5)
    fx = load_fixture(TWO_DAY_FIXTURE)
    for soc in range(25, 46, 2):
        for t in pstryk_engine.peak_topups_for_day(fx, False, _dt_at(D22, 21, 50), current_soc=soc, load_kw=1.0, **TOPUP_REAL):
            assert (t["end"] - t["start"]) >= timedelta(minutes=15), (soc, t)


# ---- code review: flash-wear hysteresis, valley floor incl. next peak floor, projection at any hour ----
RV_PRICES = [0.5] * 6 + [0.9] * 3 + [0.6] * 7 + [1.1] * 4 + [0.7] * 4  # charge 01-03, peaks 06-09 / 16-20
RV_PB = {"start": _dt_at(D23, 1).isoformat(), "end": _dt_at(D23, 3).isoformat(), "average_price": 0.5}


@pytest.mark.parametrize("start_h,soc0", [(3, 88), (10, 70), (21, 40)])
def test_hourly_resync_with_soc_drift_and_load_jitter_writes_at_most_once(start_h, soc0):
    written, writes = None, 0
    for i in range(6):
        load = 1.0 + (0.02 if i % 2 else -0.02)
        sl = _gen(RV_PRICES, soc=soc0 - i, load=load, now=_dt_at(D23, start_h + i, 5) if start_h + i < 24
                  else _dt_at(D23, 23, 55), pb=RV_PB)
        if written is None or not pstryk_engine.are_tou_slots_identical(sl, written):
            written, writes = sl, writes + 1
    assert writes <= 1, writes  # the first run writes; no hourly re-sync may rewrite the flash


def test_slot_identity_tolerates_floor_step_on_non_charge_slots_only():
    sl = _gen(RV_PRICES, soc=60, load=1.0, pb=RV_PB)
    step = energy_defaults.TOU_FLOOR_SOC_STEP
    nc = next(i for i, s in enumerate(sl) if not s["grid_charge"])
    ch = next(i for i, s in enumerate(sl) if s["grid_charge"])
    near = [dict(s, target_soc=s["target_soc"] + (step if i == nc else 0)) for i, s in enumerate(sl)]
    far = [dict(s, target_soc=s["target_soc"] + (step + 1 if i == nc else 0)) for i, s in enumerate(sl)]
    chg = [dict(s, target_soc=s["target_soc"] - (1 if i == ch else 0)) for i, s in enumerate(sl)]
    assert pstryk_engine.are_tou_slots_identical(near, sl)
    assert not pstryk_engine.are_tou_slots_identical(far, sl)
    assert not pstryk_engine.are_tou_slots_identical(chg, sl)


def test_computed_floors_snap_down_to_step_and_load_to_step():
    step = energy_defaults.TOU_FLOOR_SOC_STEP
    for load in (0.98, 1.0, 1.02):
        sl = _gen(RV_PRICES, soc=60, load=load, pb=RV_PB)
        for s in sl:
            if not s["grid_charge"]:
                assert s["target_soc"] in (20, 90) or s["target_soc"] % step == 0, s
        assert [s["target_soc"] for s in sl] == [s["target_soc"] for s in _gen(RV_PRICES, soc=60, load=1.0, pb=RV_PB)]


def test_running_topup_keeps_its_start():
    fx = load_fixture(TWO_DAY_FIXTURE)
    t0 = pstryk_engine.peak_topups_for_day(fx, False, _dt_at(D22, 22), current_soc=30, load_kw=1.0, **TOPUP_REAL)
    t1 = pstryk_engine.peak_topups_for_day(fx, False, _dt_at(D22, 23, 10), current_soc=33, load_kw=1.0, **TOPUP_REAL)
    assert t0 and t1 and t1[0]["start"] == t0[0]["start"] == _dt_at(D22, 23), (t0, t1)


def test_valley_before_reserve_peak_keeps_the_peaks_own_floor():
    # reviewer case: SOC 60 at 00:00, charge 01-03 to 90, 06-09 peak keeps a reserve for the pricier 16-20 peak
    sl = _gen(RV_PRICES, soc=60, load=2.0, pb=RV_PB)
    proj = _soc_projection(sl, D23, 60, 2.0, _dt_at(D23, 0))
    segs = [g for g in _slots_to_segments(sl, D23) if g["end"] > _dt_at(D23, 0)]
    at = lambda h: next(p for g, p in zip(segs, proj) if g["start"] <= _dt_at(D23, h) < g["end"])
    assert at(5)["soc_end"] >= at(3)["soc_start"] - 1e-6, (sl, at(3), at(5))  # 03-06 valley does not drain
    assert at(7)["soc_end"] < at(7)["soc_start"], (sl, at(7))  # the 06-09 peak gets the battery energy


def test_planner_valley_floor_mirrors_next_peak_floor():
    # planner must not assume the 03-06 valley drains (the table holds it) -> no needless top-up before 16-20
    tops = pstryk_engine.plan_peak_topups([{"frames": _curve_frames(RV_PRICES, D23)}], [RV_PB], _dt_at(D23, 0),
                                          current_soc=60, load_kw=2.0, **TOPUP_REAL)
    assert not any(_dt_at(D23, 9) <= t["start"] < _dt_at(D23, 16) for t in tops), tops


def test_topups_skipped_during_calibration():
    fx = load_fixture(TWO_DAY_FIXTURE)
    pr = {"frames": fx["pricing_frames_today"]}
    tops = pstryk_engine.peak_topups_for_day(fx, False, _dt_at(D22, 22), current_soc=30, load_kw=1.0, **TOPUP_REAL)
    assert tops
    sl = pstryk_engine.generate_deye_tou_schedule(current_soc=30, max_soc=90, min_soc=20, best_pb_window=fx["powerbank_best_window"],
        battery_capacity_kwh=CAP_KWH, nominal_voltage=VOLT, loss_buffer_pct=LOSS, pricing_data=pr, topup_windows=tops,
        max_discharge_current=140, load_kw=1.0, now_dt=_dt_at(D22, 22), calibration_active=True)
    base = pstryk_engine.generate_deye_tou_schedule(current_soc=30, max_soc=90, min_soc=20, best_pb_window=fx["powerbank_best_window"],
        battery_capacity_kwh=CAP_KWH, nominal_voltage=VOLT, loss_buffer_pct=LOSS, pricing_data=pr,
        max_discharge_current=140, load_kw=1.0, now_dt=_dt_at(D22, 22), calibration_active=True)
    assert [(s["time"], s["grid_charge"]) for s in sl] == [(s["time"], s["grid_charge"]) for s in base], sl


@pytest.mark.parametrize("day", [D23, datetime(2026, 10, 25).date()])
@pytest.mark.parametrize("hm", [(0, 30), (9, 5), (15, 30), (23, 10)])
@pytest.mark.parametrize("soc,load", [(25, 0.5), (60, 1.0), (85, 2.0)])
def test_projection_and_floors_at_any_hour(monkeypatch, day, hm, soc, load):
    now = _dt_at(day, *hm)
    freeze_now(monkeypatch, now)
    pb = {"start": _dt_at(day, 1).isoformat(), "end": _dt_at(day, 3).isoformat(), "average_price": 0.5}
    sl = _gen(RV_PRICES, soc=soc, load=load, now=now, day=day, pb=pb)
    segs = [g for g in _slots_to_segments(sl, day) if g["end"] > now]
    proj = _soc_projection(sl, day, soc, load, now)
    for g, p in zip(segs, proj):
        assert 20 - 1e-6 <= p["soc_end"] <= 90 + 1e-6 or p["soc_end"] == p["soc_start"], (g, p)
        if not g["grid_charge"]:
            assert g["target_soc"] == 20 or g["target_soc"] <= p["soc_start"] + 1e-6, (hm, g["slot"], p)


# ---- same load for dashboard and inverter: the sync records its load in the consolidated cache ----
def test_tou_load_record_reused_while_fresh(monkeypatch):
    monkeypatch.setattr(pstryk_engine, "household_load_kw", lambda *a, **k: 9.9)
    now = _dt_at(D23, 12)
    rec = {"load_kw": 0.7, "at": (now - timedelta(minutes=20)).isoformat()}
    assert pstryk_engine.tou_load_kw(rec, now, 7.0) == 0.7
    assert pstryk_engine.tou_load_kw({"load_kw": None, "at": rec["at"]}, now, 7.0) is None  # sync had no load
    stale = {"load_kw": 0.7, "at": (now - timedelta(seconds=energy_defaults.LOAD_ESTIMATE_MAX_AGE_S + 60)).isoformat()}
    assert pstryk_engine.tou_load_kw(stale, now, 7.0) == 9.9
    assert pstryk_engine.tou_load_kw({"load_kw": 0.7, "at": _dt_at(D22, 23, 50).isoformat()}, _dt_at(D23, 0, 5), 7.0) == 9.9
    assert pstryk_engine.tou_load_kw(None, now, 7.0) == 9.9


def test_sync_records_resolved_load_in_consolidated_cache(tmp_path, monkeypatch):
    cache_dir = str(tmp_path)
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}
    c_file = pstryk_engine.get_cache_file_path("dol", cache_dir)
    pstryk_engine.write_cache(c_file, {"_warsaw_date": warsaw_today_iso(), "current": {}, "today": {}, "all_prices": [],
                                       "powerbank_best_window": pb_win, "best_sell_window": sell_win})
    monkeypatch.setattr(pstryk_engine, "household_load_kw", lambda *a, **k: 0.6)
    mock_client = mock.MagicMock()
    mock_client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {"battery_soc": 50},
                                                        "active_tou": {"enabled": True, "slots": []}}
    mock_client.sync_tou_schedule.return_value = {"success": True}
    mock_client.get_configured_inverter_ip.return_value = "10.20.2.6"
    mock_client.get_configured_inverter_port.return_value = 8899
    with mock.patch("pstryk_engine.solarman_v5_client", mock_client):
        pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=cache_dir)
    rec = json.load(open(c_file))["tou_load"]
    assert rec["load_kw"] == 0.6 and pstryk_engine._iso_dt(rec["at"]) is not None
