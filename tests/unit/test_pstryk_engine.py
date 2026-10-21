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

    # Slot 4: hold the cheap midday charge (max_soc 90%) until the evening peak
    assert slots[3]["slot"] == 4
    assert slots[3]["time"] == "15:00"
    assert slots[3]["grid_charge"] is False
    assert slots[3]["target_soc"] == 90
    assert "Hold do Szczytu" in slots[3]["label"]

    # Slot 5: 100% peak coverage down to 20%
    assert slots[4]["slot"] == 5
    assert slots[4]["time"] == "17:00"
    assert slots[4]["grid_charge"] is False
    assert slots[4]["target_soc"] == 20
    assert "Szczyt Wieczorny" in slots[4]["label"]

    # Slot 6: Night autokonsumpcja / standby
    assert slots[5]["slot"] == 6
    assert slots[5]["time"] == "21:00"
    assert slots[5]["grid_charge"] is False
    assert slots[5]["target_soc"] in (20, 30)


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
    assert slots_10[5]["target_soc"] in (10, 20)

    # 2. min_soc = 20% -> morning reserve = 30%
    slots_20 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=20, max_soc=90, best_pb_window=pb_win, best_sell_window=sell_win
    )
    assert slots_20[0]["target_soc"] == 30
    assert slots_20[1]["target_soc"] == 20
    assert slots_20[5]["target_soc"] in (20, 30)

    # 3. min_soc = 25% -> morning reserve = 35%
    slots_25 = pstryk_engine.generate_deye_tou_schedule(
        min_soc=25, max_soc=90, best_pb_window=pb_win, best_sell_window=sell_win
    )
    assert slots_25[0]["target_soc"] == 35
    assert slots_25[1]["target_soc"] == 25
    assert slots_25[5]["target_soc"] in (25, 35)


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

        # Verify TOU schedule reflects safe_max_soc = 80 and morning reserve min_soc + 10 = 25
        tou_slots = consolidated.get("deye_tou_schedule_today", [])
        assert len(tou_slots) == 6
        assert tou_slots[0]["target_soc"] == 25  # Slot 1: morning reserve (15 + 10)
        assert tou_slots[1]["target_soc"] == 15  # Slot 2: min_soc
        assert tou_slots[2]["target_soc"] == 80  # Slot 3: max_soc
        assert tou_slots[3]["target_soc"] == 80  # Slot 4: hold max_soc after the midday charge until the peak
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
        morning_reserve_margin=7,
        pre_peak_margin=25,
        loss_buffer_pct=12.0,
    )
    assert res == (85, 18, 8, 55, 7, 25, 12.0)

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
    assert res_cfg[4] == energy_defaults.DEFAULT_MORNING_RESERVE_MARGIN
    assert res_cfg[5] == energy_defaults.DEFAULT_PRE_PEAK_MARGIN


def test_generate_deye_tou_schedule_variant_b_night_branch():
    """
    Verifies Variant B calculation for Night charging branch (01:00-03:00):
    - min_soc = 16%
    - morning_margin = 5% -> morning_reserve_soc = 21%
    - pre_peak_margin = 20% -> pre_peak_soc = 36%
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
        morning_reserve_margin=5,
        pre_peak_margin=20,
        best_pb_window=pb_win,
        best_sell_window=sell_win,
    )

    assert len(slots) == 6
    # Slot 1: 01:00 Cheapest Night Charge (90%, grid=True)
    assert slots[0]["slot"] == 1
    assert slots[0]["time"] == "01:00"
    assert slots[0]["grid_charge"] is True
    assert slots[0]["target_soc"] == 90

    # Slot 2: 03:00 Morning Hold (90%, grid=False)
    assert slots[1]["slot"] == 2
    assert slots[1]["time"] == "03:00"
    assert slots[1]["grid_charge"] is False
    assert slots[1]["target_soc"] == 90

    # Slot 3 & 4: Daytime hold before evening peak
    assert slots[2]["slot"] == 3
    assert slots[2]["time"] == "10:00"
    assert slots[2]["grid_charge"] is False
    assert slots[2]["target_soc"] == 90

    assert slots[3]["slot"] == 4
    assert slots[3]["time"] == "13:00"
    assert slots[3]["grid_charge"] is False
    assert slots[3]["target_soc"] == 90

    # Slot 5: 17:00 Evening Peak 100% discharge down to safe_min_soc (16%)
    assert slots[4]["slot"] == 5
    assert slots[4]["time"] == "17:00"
    assert slots[4]["grid_charge"] is False
    assert slots[4]["target_soc"] == 16
    assert "Szczyt Wieczorny" in slots[4]["label"]

    # Slot 6: 20:00 Night Standby down to safe_min_soc (16%) awaiting next day's 01:00 charging window
    assert slots[5]["slot"] == 6
    assert slots[5]["time"] == "20:00"
    assert slots[5]["grid_charge"] is False
    assert slots[5]["target_soc"] == 16
    assert "Czuwanie Nocne" in slots[5]["label"]


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
        morning_reserve_margin=5,
        pre_peak_margin=20,
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
        morning_reserve_margin=5,
        pre_peak_margin=20,
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
    """A3: a lowered live Reg 108 (50 A) must not scale the base; 4h window -> 100*2/4 = 50 A, base stays 100 A."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=20, best_pb_window=win(9, 13), best_sell_window=win(17, 20), max_charge_current=50.0,
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
        "success": True, "config": {"max_charge_current": written[-1]}, "telemetry": {}, "active_tou": {}}
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
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(1, 5), best_sell_window=win(17, 20))
    assert [s for s in slots if s["grid_charge"]][0]["target_charge_current_a"] == 50
    assert _modulate(monkeypatch, slots, 0, 30, next_cycle=50) == 50   # gap before window (was 100)
    assert _modulate(monkeypatch, slots, 3, 0, next_cycle=80) == 50    # inside window
    assert _modulate(monkeypatch, slots, 12, 0, next_cycle=50) == 100  # daytime solar
    assert _modulate(monkeypatch, slots, 22, 0, next_cycle=67) == 67   # after today's charge: next cycle pre-set


def test_modulation_window_wrapping_midnight(monkeypatch):
    """B4: a charge slot ending at 00:00 (20-24) is not treated as 'already passed' in the evening."""
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(20, 24), best_sell_window=win(17, 20))
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
    # Evening peak discharge happens before the charge window
    peak = [s for s in slots if "Szczyt Wieczorny" in s["label"]][0]
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
    """B6: a morning sell window places its discharge slot at 07:00 and still protects the 17:00 evening peak."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=20, pre_peak_margin=20, best_pb_window=win(*pb), best_sell_window=win(7, 10))
    assert_valid_schedule(slots)
    by_time = {s["time"]: s for s in slots}
    assert by_time["07:00"]["target_soc"] == 20 and not by_time["07:00"]["grid_charge"]
    # Night charge: the sale is followed by the evening peak -> keep the pre-peak reserve (40);
    # midday charge: the sale is followed by a cheap refill at 13:00 -> drain to min (20)
    assert by_time["10:00"]["target_soc"] == (40 if pb[0] < 7 else 20)
    assert by_time["17:00"]["target_soc"] == 20 and "Szczyt Wieczorny" in by_time["17:00"]["label"]
    charge_m = slot_minutes([s for s in slots if s["grid_charge"]])[0]
    assert charge_m == pb[0] * 60
    assert 7 * 60 < 17 * 60 and charge_m < 17 * 60


def test_slots_carry_nominal_voltage_for_cloud_write():
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, nominal_voltage=51.2)
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
                            if prev_k == "charge" and next_k == "peak" and slots[i]["target_soc"] != top:
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
    """M3: between the end of today's night window and 19:00 Reg 108 stays at base (100 A)."""
    slots = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(1, 5), best_sell_window=win(17, 20))
    assert _modulate(monkeypatch, slots, 5, 2, next_cycle=50) == 100
    # Night window that ended before 05:00 (02-04): 04:30 is not daytime but still before the evening
    slots_24 = pstryk_engine.generate_deye_tou_schedule(max_soc=90, min_soc=20, best_pb_window=win(2, 4), best_sell_window=win(17, 20))
    assert _modulate(monkeypatch, slots_24, 4, 0, next_cycle=50) == 100
    assert _modulate(monkeypatch, slots_24, 4, 30, next_cycle=50) == 100
    assert _modulate(monkeypatch, slots_24, 19, 0, next_cycle=50) == 50


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
    was 00:00 20 | 06:00 10 | 09:00 20 | 13:00 90 G | 15:00 20 | 17:00 10.
    (a) 15:00 must hold 90 until the 17:00 peak; (b) 09:00 must be min (next event is a cheap refill);
    (c) 00:00 keeps the morning reserve before the 06:00 sale.
    """
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=10, morning_reserve_margin=10, pre_peak_margin=10,
        best_pb_window={"start": "13:00", "end": "15:00", "duration_hours": 2},
        best_sell_window={"start": "06:00", "end": "09:00"})
    assert _sched(slots) == [
        ("00:00", 20, False), ("06:00", 10, False), ("09:00", 10, False),
        ("13:00", 90, True), ("15:00", 90, False), ("17:00", 10, False),
    ]


def test_midday_charge_13_15_sell_17_20_holds_max_until_peak():
    """Most common case: after the 13-15 charge the battery holds max until the 17:00 sell window."""
    slots = pstryk_engine.generate_deye_tou_schedule(
        max_soc=90, min_soc=10, morning_reserve_margin=10, pre_peak_margin=10,
        best_pb_window={"start": "13:00", "end": "15:00", "duration_hours": 2},
        best_sell_window={"start": "17:00", "end": "20:00"})
    assert _sched(slots) == [
        ("00:00", 20, False), ("06:00", 10, False), ("13:00", 90, True),
        ("15:00", 90, False), ("17:00", 10, False), ("20:00", 20, False),
    ]


def _ref(pb, sell, **kw):
    hm = lambda h: f"{h % 24:02d}:00"
    params = dict(max_soc=90, min_soc=10, morning_reserve_margin=10, pre_peak_margin=10)
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
            kw = dict(max_soc=90, min_soc=10, morning_reserve_margin=10, pre_peak_margin=10,
                      best_pb_window={"start": f"{pb[0]:02d}:00", "end": f"{pb[1] % 24:02d}:00", "duration_hours": pb[1] - pb[0]},
                      best_sell_window={"start": f"{sell[0]:02d}:00", "end": f"{sell[1] % 24:02d}:00"})
            a = pstryk_engine.generate_deye_tou_schedule(**kw)
            b = renamed.generate_deye_tou_schedule(**kw)
            assert [(x["time"], x["target_soc"], x["grid_charge"]) for x in a] == \
                   [(x["time"], x["target_soc"], x["grid_charge"]) for x in b], (pb, sell)
            assert any(x["label"].startswith("XSzczyt") for x in b)


def test_s1_evening_charge_19_21_reference_schedule():
    slots = _ref((19, 21), (17, 19))
    assert _sched(slots) == [
        ("00:00", 90, False), ("08:00", 20, False), ("12:00", 20, False),
        ("17:00", 10, False), ("19:00", 90, True), ("21:00", 90, False),
    ]


@pytest.mark.parametrize("pb,sell", [((19, 21), (17, 19)), ((22, 24), (17, 20)), ((20, 24), (17, 20)), ((19, 21), (7, 10))])
def test_s1_evening_charge_hold_ends_at_morning_boundary(pb, sell):
    """S1: an evening/late charge is held overnight only until morning boundary; morning boundary to sell window stays <= pre-peak reserve."""
    slots = _ref(pb, sell)
    assert_valid_schedule(slots)
    pre_peak = 10 + 10
    mb = 8 * 60 if sell[0] >= 17 else 3 * 60
    mins = slot_minutes(slots)
    active_at = lambda m: slots[max([i for i, t in enumerate(mins) if t <= m], default=5)]
    assert active_at(mb)["target_soc"] <= pre_peak
    for t, sl in zip(mins, slots):
        if mb <= t < sell[0] * 60:
            assert sl["target_soc"] <= pre_peak, (t, sl)


def test_s3_late_charge_midnight_slot_is_set_by_branch_not_wrap():
    """S3: the 00:00 slot of a 22-24 charge comes from the branch itself, not from wrapping 'prev' to today's charge."""
    s0 = _ref((22, 24), (17, 20))[0]
    assert s0["time"] == "00:00"
    assert s0["label"] not in STRETCH_LABELS
    assert s0["role"] != "charge"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])



