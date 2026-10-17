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


def test_consolidated_smart_cache_lifecycle(tmp_path):
    """
    Verifies smart file caching in /tmp/pstryk_cache_{installation}.json:
    - 15-minute TTL cache hit returns instantly without API requests.
    - Cache expiration (> 15 min) triggers a refresh.
    - Corrupt cache file is safely removed and refreshed.
    """
    cache_dir = str(tmp_path)
    cache_file = pstryk_engine.get_cache_file_path("gora", cache_dir=cache_dir)
    hourly_payload = load_fixture("sample_hourly_today.json")

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
    # Slot 1: Night charge (02:00)
    assert slots[0]["slot"] == 1
    assert slots[0]["time"] == "02:00"
    assert slots[0]["grid_charge"] is True
    assert slots[0]["target_soc"] == 90

    # Slot 2: Morning hold (04:00)
    assert slots[1]["slot"] == 2
    assert slots[1]["time"] == "04:00"
    assert slots[1]["grid_charge"] is False
    assert slots[1]["target_soc"] == 90

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
    )
    assert slots[0]["target_soc"] == 100
    assert "Kalibracja" in slots[0]["label"]


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
    )
    assert slots[2]["time"] == "13:00"
    assert slots[2]["grid_charge"] is True
    assert "Tania Godzina" in slots[2]["label"]


def test_sync_deye_inverter_tou_schedule_integration():
    """Verifies sync_deye_inverter_tou_schedule calls solarman client with 6 slots."""
    with mock.patch("solarman_v5_client.read_inverter_telemetry") as mock_read, \
         mock.patch("solarman_v5_client.sync_tou_schedule") as mock_sync:
        mock_read.return_value = {
            "success": True,
            "config": {"max_charge_soc": 90, "min_discharge_soc": 20},
            "telemetry": {"battery_soc": 82},
        }
        mock_sync.return_value = {"success": True, "message": "OK"}

        res = pstryk_engine.sync_deye_inverter_tou_schedule(host="10.20.2.6", port=8899)
        assert res["success"] is True
        assert len(res["slots"]) == 6
        assert res["soc_limits"]["current_soc"] == 82
        mock_sync.assert_called_once()


def test_cli_sync_deye_tou_flag():
    """Verifies CLI flag --sync-deye-tou executes cleanly."""
    with mock.patch("solarman_v5_client.sync_tou_schedule") as mock_sync, \
         mock.patch("solarman_v5_client.read_inverter_telemetry") as mock_read:
        mock_read.return_value = {"success": True, "config": {}, "telemetry": {}}
        mock_sync.return_value = {"success": True, "message": "OK"}

        ret = pstryk_engine.main(["--sync-deye-tou", "--inverter-host", "127.0.0.1"])
        assert ret == 0


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
    # Slot 1: Night standby holding morning reserve (min_soc + 10% = 30%)
    assert slots[0]["slot"] == 1
    assert slots[0]["time"] == "00:00"
    assert slots[0]["grid_charge"] is False
    assert slots[0]["target_soc"] == 30
    assert "Rezerwa na Szczyt Poranny" in slots[0]["label"]

    # Slot 2: Morning self-consumption down to min_soc (20%)
    assert slots[1]["slot"] == 2
    assert slots[1]["time"] == "06:00"
    assert slots[1]["grid_charge"] is False
    assert slots[1]["target_soc"] == 20

    # Slot 3: Midday grid charge at 13:00
    assert slots[2]["slot"] == 3
    assert slots[2]["time"] == "13:00"
    assert slots[2]["grid_charge"] is True
    assert slots[2]["target_soc"] == 90
    assert "Najtańsze Ładowanie" in slots[2]["label"]

    # Slot 4: Pre-peak daytime autokonsumpcja (70%)
    assert slots[3]["slot"] == 4
    assert slots[3]["time"] == "15:00"
    assert slots[3]["grid_charge"] is False
    assert slots[3]["target_soc"] == 70
    assert "Autokonsumpcja PV" in slots[3]["label"]

    # Slot 5: 100% peak coverage down to 20%
    assert slots[4]["slot"] == 5
    assert slots[4]["time"] == "17:00"
    assert slots[4]["grid_charge"] is False
    assert slots[4]["target_soc"] == 20
    assert "Szczyt Wieczorny" in slots[4]["label"]

    # Slot 6: Night autokonsumpcja / morning reserve standby (30%)
    assert slots[5]["slot"] == 6
    assert slots[5]["time"] == "21:00"
    assert slots[5]["grid_charge"] is False
    assert slots[5]["target_soc"] == 30


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
        "has_tomorrow_pricing": True,
        "powerbank_best_window": {"start": "2026-10-08T11:00:00Z", "end": "2026-10-08T13:00:00Z", "is_consecutive": True},
        "best_sell_window": {"start": "2026-10-08T15:00:00Z", "end": "2026-10-08T19:00:00Z"},
        "powerbank_best_window_tomorrow": {"start": "2026-10-09T11:00:00Z", "end": "2026-10-09T13:00:00Z", "is_consecutive": True},
        "best_sell_window_tomorrow": {"start": "2026-10-09T16:00:00Z", "end": "2026-10-09T20:00:00Z"},
    }
    pstryk_engine.write_cache(pstryk_engine.get_cache_file_path("dol", cache_dir), cached_payload)

    with mock.patch("pstryk_engine.solarman_v5_client", None):
        result = pstryk_engine.sync_deye_inverter_tou_schedule(
            cache_dir=cache_dir,
            for_tomorrow=True,
        )
    assert len(result["slots"]) == 6


def test_powerbank_weekend_auto_duration_and_weekday_fixed():
    """Verifies that find_powerbank_best_window defaults to 2h on weekdays and expands on weekends."""
    # Weekday (Friday): 2026-10-16
    friday_dt = datetime(2026, 10, 16, 12, 0, tzinfo=pstryk_engine.WARSAW_TZ)
    base_fri = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)
    sample_frames = []
    for h in range(24):
        st = (base_fri + timedelta(hours=h)).isoformat()
        et = (base_fri + timedelta(hours=h + 1)).isoformat()
        # Midday cheap price
        price = 0.50 if 11 <= h <= 15 else 1.20
        sample_frames.append({"start": st, "end": et, "metrics": {"pricing": {"price_gross": price}}})

    res_weekday = pstryk_engine.find_powerbank_best_window({"frames": sample_frames}, target_hours="auto", now_dt=friday_dt)
    assert res_weekday["duration_hours"] == 2

    # Weekend (Saturday): 2026-10-17
    saturday_dt = datetime(2026, 10, 17, 12, 0, tzinfo=pstryk_engine.WARSAW_TZ)
    base_sat = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
    sat_frames = []
    for h in range(24):
        st = (base_sat + timedelta(hours=h)).isoformat()
        et = (base_sat + timedelta(hours=h + 1)).isoformat()
        # Midday solar dip 11:00 - 15:00 (4 hours)
        price = 0.50 if 11 <= h <= 14 else 1.20
        sat_frames.append({"start": st, "end": et, "metrics": {"pricing": {"price_gross": price}}})

    res_weekend = pstryk_engine.find_powerbank_best_window({"frames": sat_frames}, target_hours="auto", now_dt=saturday_dt)
    assert res_weekend["duration_hours"] >= 4
    assert res_weekend["is_consecutive"] is True


def test_proportional_charging_power_scaling_and_slot5_protection():
    """
    Verifies that TOU power_w across all slots strictly stays at base discharge power (7170W for 140A * 51.2V),
    while the charging slot dynamically modulates target_charge_current_a (Register 108) based on window duration
    (50A for 4h window, 100A for 2h window), and Slot 5 (Peak Evening Discharge) strictly retains full base power.
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
    )
    charge_slot_50 = [s for s in slots_50 if s["grid_charge"] is True][0]
    assert charge_slot_50["power_w"] == 7170
    assert charge_slot_50["target_charge_current_a"] == 50
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
    )
    charge_slot_20 = [s for s in slots_20 if s["grid_charge"] is True][0]
    assert charge_slot_20["power_w"] == 7170
    assert charge_slot_20["target_charge_current_a"] == 50

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
    )
    charge_slot_2h = [s for s in slots_2h if s["grid_charge"] is True][0]
    assert charge_slot_2h["power_w"] == 7170
    assert charge_slot_2h["target_charge_current_a"] == 100


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


def test_powerbank_weekend_tolerance_expansion_user_example():
    """
    Verifies the user's explicit requirement:
    - Base 12:00-14:00 (2h) has average price 0.90 zł/kWh.
    - If 14:00-15:00 is 0.93 zł/kWh (<= 0.90 + 0.05 = 0.95), expand to 3h window (12:00-15:00) with avg 0.91 zł/kWh.
    - If 14:00-15:00 is 0.96 zł/kWh (> 0.95), keep 2h window (12:00-14:00) with avg 0.90 zł/kWh.
    """
    saturday_dt = datetime(2026, 10, 17, 12, 0, tzinfo=pstryk_engine.WARSAW_TZ)
    base_sat = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)

    # Case A: 14:00-15:00 is 0.93 zł/kWh -> expands to 3h (avg 0.91)
    frames_a = []
    for h in range(24):
        st = (base_sat + timedelta(hours=h)).isoformat()
        et = (base_sat + timedelta(hours=h + 1)).isoformat()
        if h in (12, 13):
            p = 0.90
        elif h == 14:
            p = 0.93
        else:
            p = 1.20
        frames_a.append({"start": st, "end": et, "metrics": {"pricing": {"price_gross": p}}})

    res_a = pstryk_engine.find_powerbank_best_window({"frames": frames_a}, target_hours="auto", now_dt=saturday_dt)
    assert res_a["duration_hours"] == 3
    assert res_a["average_price"] == 0.91
    assert res_a["is_consecutive"] is True

    # Case B: 14:00-15:00 is 0.96 zł/kWh -> stays 2h (avg 0.90)
    frames_b = []
    for h in range(24):
        st = (base_sat + timedelta(hours=h)).isoformat()
        et = (base_sat + timedelta(hours=h + 1)).isoformat()
        if h in (12, 13):
            p = 0.90
        elif h == 14:
            p = 0.96
        else:
            p = 1.20
        frames_b.append({"start": st, "end": et, "metrics": {"pricing": {"price_gross": p}}})

    res_b = pstryk_engine.find_powerbank_best_window({"frames": frames_b}, target_hours="auto", now_dt=saturday_dt)
    assert res_b["duration_hours"] == 2
    assert res_b["average_price"] == 0.90
    assert res_b["is_consecutive"] is True


def test_save_last_tou_sync_result(tmp_path):
    """Verifies that save_last_tou_sync_result persists JSON data properly."""
    test_file = tmp_path / ".deye_tou_last_sync.json"
    with mock.patch("pstryk_engine.os.path.join", return_value=str(test_file)):
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


def test_dynamic_morning_reserve_adapting_to_min_soc():
    """
    Verifies that morning reserve dynamically evaluates to min_soc + 10%
    for different hardware configurations (10%, 20%, 25%).
    """
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    # 1. min_soc = 10% -> morning reserve = 20%
    slots_10 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=10, max_soc=90, best_pb_window=pb_win, best_sell_window=sell_win
    )
    assert slots_10[0]["target_soc"] == 20
    assert slots_10[1]["target_soc"] == 10  # Morning peak discharges down to min_soc
    assert slots_10[5]["target_soc"] == 20

    # 2. min_soc = 20% -> morning reserve = 30%
    slots_20 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=20, max_soc=90, best_pb_window=pb_win, best_sell_window=sell_win
    )
    assert slots_20[0]["target_soc"] == 30
    assert slots_20[1]["target_soc"] == 20
    assert slots_20[5]["target_soc"] == 30

    # 3. min_soc = 25% -> morning reserve = 35%
    slots_25 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=25, max_soc=90, best_pb_window=pb_win, best_sell_window=sell_win
    )
    assert slots_25[0]["target_soc"] == 35
    assert slots_25[1]["target_soc"] == 25
    assert slots_25[5]["target_soc"] == 35


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


def test_fetch_consolidated_custom_soc_and_battery_model(tmp_path):
    """
    Asserts that fetch_consolidated_data correctly adopts user-supplied max_soc,
    min_soc, and current_soc into both BatteryModel and the generated TOU schedules.
    """
    cache_dir = str(tmp_path / "cache_soc")
    os.makedirs(cache_dir, exist_ok=True)

    dummy_payload = {
        "frames": [
            {
                "start": "2026-10-16T10:00:00Z",
                "end": "2026-10-16T11:00:00Z",
                "is_live": False,
                "metrics": {
                    "pricing": {"price_gross": 0.80, "price_net": 0.65},
                    "cost": {"cost_gross": 0.80, "cost_net": 0.65},
                    "meter_values": {"kwh_import": 1.0, "kwh_export": 0.0},
                    "carbon": {"carbon_g": 500.0},
                },
            }
        ]
    }

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

        # Verify TOU schedule reflects safe_max_soc = 80 and morning reserve min_soc + 10 = 25
        tou_slots = consolidated.get("deye_tou_schedule_today", [])
        assert len(tou_slots) == 6
        assert tou_slots[0]["target_soc"] == 25  # Slot 1: morning reserve (15 + 10)
        assert tou_slots[1]["target_soc"] == 15  # Slot 2: min_soc
        assert tou_slots[2]["target_soc"] == 80  # Slot 3: max_soc
        assert tou_slots[3]["target_soc"] == 60  # Slot 4: pre_peak autokonsumpcja (max_soc - 20)
        assert tou_slots[4]["target_soc"] == 15  # Slot 5: evening peak discharge down to min_soc
        assert tou_slots[5]["target_soc"] == 25  # Slot 6: night standby reserve (15 + 10)


def test_dynamic_capacity_scaling_custom_kwh():
    """
    Verifies that battery_capacity_kwh dynamically scales energy needed and charging power.
    Compares 16.13 kWh with 32.26 kWh (e.g. 2 parallel SunDeposit packs).
    """
    pb_4h = {"start": "2026-10-17T11:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 4, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    # 1. Standard pack:
    # TOU power_w is base discharge power (7170W for 140A * 51.2V)
    # Register 108 target charging current scaled for 4h: 100A * (2 / 4) = 50A
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
    assert charge_doubled["target_charge_current_a"] == 50

    # 3. 2h window with 140A charge current:
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
    assert charge_doubled_2h["target_charge_current_a"] == 140


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


def test_custom_morning_reserve_margin_and_clamping():
    """
    Verifies that morning_reserve_margin is dynamic and safely clamped:
    morning_reserve_soc = min(safe_max_soc, safe_min_soc + max(0, margin)).
    """
    pb_win = {"start": "2026-10-17T13:00:00Z", "end": "2026-10-17T15:00:00Z", "duration_hours": 2, "is_consecutive": True}
    sell_win = {"start": "2026-10-17T17:00:00Z", "end": "2026-10-17T21:00:00Z"}

    # 1. 5% margin with min_soc = 20 -> 25%
    s_5 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=20, max_soc=90, morning_reserve_margin=5,
        best_pb_window=pb_win, best_sell_window=sell_win,
    )
    assert s_5[0]["target_soc"] == 25
    assert s_5[5]["target_soc"] == 25

    # 2. 15% margin with min_soc = 20 -> 35%
    s_15 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=20, max_soc=90, morning_reserve_margin=15,
        best_pb_window=pb_win, best_sell_window=sell_win,
    )
    assert s_15[0]["target_soc"] == 35
    assert s_15[5]["target_soc"] == 35

    # 3. 25% margin with min_soc = 20 and max_soc = 40 (clamped to 50 max_soc lower bound in generate)
    # min_soc + margin = 20 + 25 = 45 -> clamped to safe_max_soc (50)
    s_clamp = pstryk_engine.generate_deye_tou_schedule(
        min_soc=20, max_soc=50, morning_reserve_margin=35,
        best_pb_window=pb_win, best_sell_window=sell_win,
    )
    assert s_clamp[0]["target_soc"] == 50
    assert s_clamp[5]["target_soc"] == 50


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


