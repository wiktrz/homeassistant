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

# Ensure config/ directory is importable
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "pstryk")

if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

import pstryk_engine


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
        assert len(today["hourly"]) == 24
        assert today["hourly"][0]["start"] == "00:00"
        assert today["hourly"][0]["end"] == "01:00"
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
    assert "13:00 - 15:00" in ev_weekday["display"]
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

    # In sample_hourly_today, the two lowest hours are 13:00 (0.30) and 14:00 (0.31) -> contiguous!
    pb_res = pstryk_engine.find_powerbank_best_window(hourly_payload, target_hours=2, allow_disjoint=True)
    assert pb_res["duration_hours"] == 2
    assert pb_res["is_consecutive"] is True
    assert pb_res["display"] == "13:00 - 15:00 (śr. 0.30 zł/kWh)"

    # Test synthetic disjoint frames (e.g. cheapest at 03:00 and 14:00)
    synthetic_frames = [
        {"start": "2026-10-06T03:00:00Z", "end": "2026-10-06T04:00:00Z", "metrics": {"pricing": {"price_gross": 0.20}}},
        {"start": "2026-10-06T04:00:00Z", "end": "2026-10-06T05:00:00Z", "metrics": {"pricing": {"price_gross": 0.80}}},
        {"start": "2026-10-06T14:00:00Z", "end": "2026-10-06T15:00:00Z", "metrics": {"pricing": {"price_gross": 0.25}}},
        {"start": "2026-10-06T15:00:00Z", "end": "2026-10-06T16:00:00Z", "metrics": {"pricing": {"price_gross": 0.90}}},
    ]
    disjoint_res = pstryk_engine.find_powerbank_best_window({"frames": synthetic_frames}, target_hours=2, allow_disjoint=True)
    assert disjoint_res["duration_hours"] == 2
    assert disjoint_res["is_consecutive"] is False
    assert disjoint_res["slots"] == ["03:00 - 04:00", "14:00 - 15:00"]
    assert disjoint_res["display"] == "03:00 - 04:00 oraz 14:00 - 15:00 (śr. 0.23 zł/kWh)"


def test_best_sell_window_and_spike_calculation():
    """Verifies peak selling window and 1-hour absolute spike calculation."""
    hourly_payload = load_fixture("sample_hourly_today.json")
    sell_res = pstryk_engine.find_best_sell_window(hourly_payload, target_hours=3)

    assert sell_res["duration_hours"] == 3
    assert sell_res["start"] == "2026-09-24T18:00:00Z"
    assert sell_res["end"] == "2026-09-24T21:00:00Z"
    assert sell_res["spike_hour"] == "19:00 - 20:00"
    assert sell_res["spike_price"] == 0.799
    assert "18:00 - 21:00" in sell_res["display"]
    assert "śr. 0.75 zł/kWh" in sell_res["display"]
    assert "pik 19:00: 0.80 zł" in sell_res["display"]


def test_generate_deye_tou_schedule_structure_and_monotonicity():
    """Verifies that generate_deye_tou_schedule produces 6 monotonic slots with valid clamps."""
    pb_win = {"start": "2026-10-06T02:00:00Z", "end": "2026-10-06T04:00:00Z", "is_consecutive": True}
    sell_win = {"start": "2026-10-06T17:00:00Z", "end": "2026-10-06T21:00:00Z"}

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


