#!/usr/bin/env python3
"""TDD (must fail until implemented): Deye Cloud read-only history client + load profile + rolling planner.

ASSUMED SIGNATURES
  deye_cloud_client.list_stations(cfg=None) -> list[dict]            (POST /v1.0/station/list, {"page":1,"size":200})
  deye_cloud_client.fetch_station_history_day(station_id, day: date, cfg=None) -> list[dict] | None
      frames {"ts": aware datetime (UTC), "consumption_w","battery_w","soc","generation_w","grid_w"}; None on API failure
      body {"stationId","granularity":1,"startAt","endAt"(day+1)}; on error retries with startDate/endDate
  deye_cloud_client.fetch_consumption_history(days, cfg=None, cache_path=None, now=None) -> list[frames] | None
  pstryk_engine.build_load_profile(history) -> {"weekday":[24],"weekend":[24]} kWh/h | None
  pstryk_engine.load_forecast(start, end, profile, live_kw=None) -> [{"start": aware dt, "kwh": float}] per Warsaw hour
  pstryk_engine.plan_rolling_tou(frames, now_dt, current_soc, profile, *, min_soc, max_soc, capacity_kwh, voltage,
      ceiling_a, loss_pct, **kw) -> {"plan_timeline":[{"start","end" ISO,"role" in charge|topup|peak|hold|drain,
      "target_soc","soc_end","charge_a","price"}], "deye_tou_schedule":[6 {"time":"HH:MM","target_soc","grid_charge",...}],
      "horizon_end": ISO}
"""
import os, sys, json, copy
import pytest
import unittest.mock as mock
from datetime import datetime, timedelta, timezone, date

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(ROOT, "config"))
import deye_cloud_client as dcc
import pstryk_engine as pe

FX = os.path.join(ROOT, "tests", "fixtures")
W = pe.WARSAW_TZ


def fx(*p):
    with open(os.path.join(FX, *p), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _no_net(monkeypatch):
    def boom(*a, **k):
        raise OSError("network disabled")
    monkeypatch.setattr(dcc.urllib.request, "urlopen", boom)


def api(responses, calls):
    """Fake call_deye_api: responses = fn(endpoint, data) -> (ok, json, msg)."""
    def f(endpoint, method="GET", data=None, cfg=None):
        calls.append((endpoint, method, data))
        return responses(endpoint, data)
    return f


# ---------------- 1. Deye client ----------------
def test_list_stations():
    calls = []
    fake = api(lambda e, d: (True, fx("deye", "station_list.json"), "OK"), calls)
    with mock.patch.object(dcc, "call_deye_api", fake):
        st = dcc.list_stations()
    assert st[0]["id"] == 61234567
    assert calls[0][0] == "/v1.0/station/list" and calls[0][1] == "POST"
    assert calls[0][2] == {"page": 1, "size": 200}


def test_history_day_parses_mixed_timestamps_and_request_body():
    calls = []
    fake = api(lambda e, d: (True, fx("deye", "history_station_day.json"), "OK"), calls)
    with mock.patch.object(dcc, "call_deye_api", fake):
        fr = dcc.fetch_station_history_day(61234567, date(2026, 10, 21))
    assert len(fr) == 4
    assert all(f["ts"].tzinfo is not None for f in fr)
    assert [f["ts"] for f in fr] == sorted(f["ts"] for f in fr)
    assert fr[0]["consumption_w"] == 600 and fr[0]["soc"] == 55
    assert fr[2]["ts"] - fr[1]["ts"] == timedelta(seconds=300)  # ms parsed
    assert fr[3]["ts"] - fr[2]["ts"] == timedelta(seconds=300)  # str parsed
    ep, m, body = calls[0]
    assert ep == "/v1.0/station/history" and m == "POST"
    assert body == {"stationId": 61234567, "granularity": 1, "startAt": "2026-10-21", "endAt": "2026-10-22"}


def test_history_day_falls_back_to_startdate_enddate():
    calls = []

    def resp(e, d):
        if "startAt" in d:
            return False, None, "HTTP Error 400: bad param"
        return True, fx("deye", "history_station_day.json"), "OK"
    with mock.patch.object(dcc, "call_deye_api", api(resp, calls)):
        fr = dcc.fetch_station_history_day(1, date(2026, 10, 21))
    assert len(fr) == 4
    assert calls[1][2] == {"stationId": 1, "granularity": 1, "startDate": "2026-10-21", "endDate": "2026-10-22"}


def test_history_day_failure_returns_none():
    with mock.patch.object(dcc, "call_deye_api", api(lambda e, d: (False, None, "x"), [])):
        assert dcc.fetch_station_history_day(1, date(2026, 10, 21)) is None


def _router(fail_day=None):
    def resp(e, d):
        if e == "/v1.0/station/list":
            return True, fx("deye", "station_list.json"), "OK"
        if fail_day and (d.get("startAt") == fail_day or d.get("startDate") == fail_day):
            return False, None, "boom"
        return True, fx("deye", "history_station_day.json"), "OK"
    return resp


def _hist_calls(calls):
    return [c for c in calls if c[0] == "/v1.0/station/history"]


def test_fetch_history_stops_at_first_failing_day_newest_first(tmp_path):
    calls, cache = [], str(tmp_path / "h.json")
    now = datetime(2026, 10, 22, 12, tzinfo=W)
    with mock.patch.object(dcc, "call_deye_api", api(_router("2026-10-20"), calls)):
        h = dcc.fetch_consumption_history(3, cache_path=cache, now=now)
    days = [c[2].get("startAt") or c[2].get("startDate") for c in _hist_calls(calls)]
    assert "2026-10-19" not in days and "2026-10-21" in days and h  # stopped after 10-20 failed both variants
    assert len([d for d in days if d == "2026-10-20"]) == 2
    assert not any("/order/" in c[0] or "update" in c[0] for c in calls)


def test_fetch_history_refreshes_at_most_daily(tmp_path):
    cache = str(tmp_path / "h.json")
    calls = []
    with mock.patch.object(dcc, "call_deye_api", api(_router(), calls)):
        dcc.fetch_consumption_history(2, cache_path=cache, now=datetime(2026, 10, 22, 8, tzinfo=W))
        n = len(calls)
        h2 = dcc.fetch_consumption_history(2, cache_path=cache, now=datetime(2026, 10, 22, 20, tzinfo=W))
        assert len(calls) == n and h2  # same day: cache reused, no API
        dcc.fetch_consumption_history(2, cache_path=cache, now=datetime(2026, 10, 23, 8, tzinfo=W))
        assert len(calls) > n  # next day refreshes


def test_fetch_history_same_day_reuse_even_when_frames_unchanged(tmp_path):
    cache, calls = str(tmp_path / "h.json"), []
    with mock.patch.object(dcc, "call_deye_api", api(_router(), calls)):
        dcc.fetch_consumption_history(1, cache_path=cache, now=datetime(2026, 10, 22, 8, tzinfo=W))
        dcc.fetch_consumption_history(1, cache_path=cache, now=datetime(2026, 10, 23, 8, tzinfo=W))
        assert json.load(open(cache))["fetched_date"] == "2026-10-23"
        n = len(calls)
        dcc.fetch_consumption_history(1, cache_path=cache, now=datetime(2026, 10, 23, 20, tzinfo=W))
        assert len(calls) == n


def test_fetch_history_all_failed_returns_none_and_writes_negative_marker(tmp_path):
    cache = str(tmp_path / "h.json")
    now = datetime(2026, 10, 22, 8, tzinfo=W)
    calls = []
    with mock.patch.object(dcc, "call_deye_api", api(lambda e, d: (e == "/v1.0/station/list" and True, fx("deye", "station_list.json") if e == "/v1.0/station/list" else None, "x"), calls)):
        assert dcc.fetch_consumption_history(2, cache_path=cache, now=now) is None
        m = json.load(open(cache))
        assert m["failed"] is True and m["last_attempt"]
        n = len(calls)
        # within retry window: no API call at all
        assert dcc.fetch_consumption_history(2, cache_path=cache, now=now + timedelta(hours=1)) is None
        assert len(calls) == n
        # after DEYE_HISTORY_RETRY_HOURS: retries
        dcc.fetch_consumption_history(2, cache_path=cache, now=now + timedelta(hours=dcc.energy_defaults.DEYE_HISTORY_RETRY_HOURS + 1))
        assert len(calls) > n


def test_fetch_history_remembers_working_variant(tmp_path):
    cache, calls = str(tmp_path / "h.json"), []

    def resp(e, d):
        if e == "/v1.0/station/list":
            return True, fx("deye", "station_list.json"), "OK"
        if "startAt" in d:
            return False, None, "400"
        return True, fx("deye", "history_station_day.json"), "OK"
    with mock.patch.object(dcc, "call_deye_api", api(resp, calls)):
        dcc.fetch_consumption_history(3, cache_path=cache, now=datetime(2026, 10, 22, 8, tzinfo=W))
    assert json.load(open(cache))["variant"] == "startDate"
    hc = _hist_calls(calls)
    assert sum(1 for c in hc if "startAt" in c[2]) == 1  # probed once, then startDate first


def test_load_cached_history_never_calls_api(tmp_path):
    cache = str(tmp_path / "h.json")
    with mock.patch.object(dcc, "call_deye_api", api(_router(), [])):
        dcc.fetch_consumption_history(1, cache_path=cache, now=datetime(2026, 10, 22, 8, tzinfo=W))
    with mock.patch.object(dcc, "call_deye_api", side_effect=AssertionError("net")):
        assert dcc.load_cached_history(cache)
        assert dcc.load_cached_history(str(tmp_path / "missing.json")) is None


def test_engine_profile_and_plan_fields_read_cache_only(tmp_path, monkeypatch):
    cache = str(tmp_path / "h.json")
    with mock.patch.object(dcc, "call_deye_api", api(_router(), [])):
        dcc.fetch_consumption_history(1, cache_path=cache, now=datetime(2026, 10, 22, 8, tzinfo=W))
    monkeypatch.setenv("DEYE_LOAD_HISTORY_FILE", cache)
    boom = mock.Mock(side_effect=AssertionError("engine must not fetch history"))
    with mock.patch.object(dcc, "call_deye_api", boom), mock.patch.object(dcc, "fetch_consumption_history", boom), \
            mock.patch.object(dcc, "list_stations", boom):
        assert pe.load_household_profile() is not None
        pe.rolling_plan_fields({"pricing_frames": []}, current_soc=50, min_soc=10, max_soc=100, capacity_kwh=10,
                               voltage=51.2, loss_pct=10, calibration_active=False, max_discharge_current=None)
    monkeypatch.setenv("DEYE_LOAD_HISTORY_FILE", str(tmp_path / "none.json"))
    assert pe.load_household_profile() is None


# ---------------- 2. profile / forecast ----------------
def _frames(day, hour_w, step=5):
    """5-min frames for a Warsaw day; hour_w(h)->W (None skips the hour)."""
    t = datetime(day.year, day.month, day.day, tzinfo=W)
    out = []
    while t.date() == day:
        w = hour_w(t.hour)
        if w is not None:
            out.append({"ts": t.astimezone(timezone.utc), "consumption_w": w})
        t += timedelta(minutes=step)
    return out


def test_profile_averages_per_warsaw_hour_weekday_weekend():
    # Mon 2026-10-19 (weekday) 1000W flat, Sat 2026-10-24 (weekend) 500W flat
    h = _frames(date(2026, 10, 19), lambda hr: 1000) + _frames(date(2026, 10, 24), lambda hr: 500)
    p = pe.build_load_profile(h)
    assert len(p["weekday"]) == len(p["weekend"]) == 24
    assert all(abs(x - 1.0) < 1e-6 for x in p["weekday"])
    assert all(abs(x - 0.5) < 1e-6 for x in p["weekend"])


def test_profile_missing_hours_use_overall_mean_and_empty_is_none():
    h = _frames(date(2026, 10, 19), lambda hr: 1000 if hr < 12 else (2000 if hr == 12 else None))
    p = pe.build_load_profile(h)
    assert abs(p["weekday"][5] - 1.0) < 1e-6 and abs(p["weekday"][12] - 2.0) < 1e-6
    mean = (12 * 1.0 + 2.0) / 13
    assert abs(p["weekday"][20] - mean) < 0.2  # filled from overall mean of history
    assert pe.build_load_profile([]) is None and pe.build_load_profile(None) is None


def test_profile_is_dst_correct():
    # 2026-10-25 is the fall-back day (25 h): local hour 2 occurs twice; each hour bucket still 1 kWh/h at 1000 W
    h = _frames(date(2026, 10, 25), lambda hr: 1000, step=5)
    p = pe.build_load_profile(h)
    assert all(abs(x - 1.0) < 1e-6 for x in p["weekend"])


def test_load_forecast_per_hour_weekday_weekend_by_warsaw_date():
    prof = {"weekday": [float(i) for i in range(24)], "weekend": [10.0 + i for i in range(24)]}
    # Fri 23.10 22:00 -> Sat 24.10 02:00 Warsaw
    s = datetime(2026, 10, 23, 22, tzinfo=W)
    f = pe.load_forecast(s, s + timedelta(hours=4), prof)
    assert [round(x["kwh"], 3) for x in f] == [22.0, 23.0, 10.0, 11.0]


def test_load_forecast_live_kw_only_affects_current_hour():
    prof = {"weekday": [1.0] * 24, "weekend": [1.0] * 24}
    s = datetime(2026, 10, 22, 10, 30, tzinfo=W)
    a = pe.load_forecast(s, s + timedelta(hours=3), prof)
    b = pe.load_forecast(s, s + timedelta(hours=3), prof, live_kw=3.0)
    assert [x["kwh"] for x in a[1:]] == [x["kwh"] for x in b[1:]]


# ---------------- 3/4/5. rolling planner ----------------
PROFILE_DAY = [0.115, 0.101, 0.095, 0.107, 0.096, 0.095, 0.097, 0.166, 0.161, 0.192, 0.235, 0.207, 0.211, 0.514,
               0.338, 0.691, 0.354, 0.244, 0.306, 0.400, 0.352, 0.282, 0.203, 0.143]
CAP, VOLT, LOSS = 16.13, 51.2, 10.0


def prof(k=1.0):
    p = [x * k for x in PROFILE_DAY]
    return {"weekday": p, "weekend": p}


def all_frames():
    d = fx("pstryk", "two_day_2026_10_22_23.json")
    return d["pricing_frames_today"] + d["pricing_frames_tomorrow"]


def plan(now, soc, profile, frames=None, **kw):
    return pe.plan_rolling_tou(frames or all_frames(), now, soc, profile, min_soc=20, max_soc=90, **kw,
                               capacity_kwh=CAP, voltage=VOLT, ceiling_a=100.0, loss_pct=LOSS)


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def role_at(tl, t):
    for e in tl:
        if iso(e["start"]) <= t < iso(e["end"]):
            return e
    return None


NOW = datetime(2026, 10, 22, 22, 5, tzinfo=W)


def test_timeline_continuous_across_midnight_to_last_priced_hour():
    r = plan(NOW, 60, prof())
    tl = r["plan_timeline"]
    assert iso(tl[0]["start"]) <= NOW < iso(tl[0]["end"])
    for a, b in zip(tl, tl[1:]):
        assert iso(a["end"]) == iso(b["start"])
    pub = [e for e in tl if not e.get("estimated")]
    assert iso(pub[-1]["end"]) == iso(r["horizon_end"]) == datetime(2026, 10, 24, 0, 0, tzinfo=W).astimezone(timezone.utc)  # end of the last priced hour (23:00-24:00)
    assert iso(tl[-1]["end"]) == iso(r["horizon_end"])  # estimate_beyond_horizon is off by default: no estimated day


def test_replan_at_2205_with_projected_soc_keeps_the_1745_plan_beyond_now():
    """Greedy plans depend on the SOC: a re-plan at 22:05 from the SOC the 17:45 plan projects there keeps the same
    behaviour (roles, floors, charge hours) for the rest of the horizon."""
    a = plan(datetime(2026, 10, 22, 17, 45, tzinfo=W), 60, prof())["plan_timeline"]
    e = role_at(a, NOW)
    soc = e["soc_start"] + (e["soc_end"] - e["soc_start"]) * ((NOW - iso(e["start"])) / (iso(e["end"]) - iso(e["start"])))
    b = plan(NOW, soc, prof())["plan_timeline"]
    t0 = datetime(2026, 10, 22, 23, 0, tzinfo=W)
    for m in range(0, 24 * 60, 30):
        t = t0 + timedelta(minutes=m)
        if t >= datetime(2026, 10, 23, 23, tzinfo=W):
            break
        ea, eb = role_at(a, t), role_at(b, t)
        assert ea and eb and (ea["role"], ea["target_soc"], ea["charge_a"]) == (eb["role"], eb["target_soc"], eb["charge_a"]), t


@pytest.mark.parametrize("soc", [30, 60, 90])
def test_table_six_strict_slots_and_matches_timeline(soc):
    r = plan(NOW, soc, prof())
    sl = r["deye_tou_schedule"]
    assert len(sl) == 6
    mins = [int(s["time"][:2]) * 60 + int(s["time"][3:]) for s in sl]
    assert mins == sorted(mins) and len(set(mins)) == 6
    for k in range(0, 24 * 60, 15):
        t = NOW + timedelta(minutes=k)
        cm = (t.hour * 60 + t.minute)
        idx = max([i for i, m in enumerate(mins) if m <= cm], default=5)
        e = role_at(r["plan_timeline"], t)
        assert bool(sl[idx]["grid_charge"]) == (e["role"] in ("charge", "topup")), (t, e)


def test_peaks_classified_per_price_day():
    tl = plan(NOW, 60, prof())["plan_timeline"]
    for day in (22, 23):
        fr = [f for f in all_frames() if iso(f["start"]).astimezone(W).day == day]
        pr = [f["metrics"]["pricing"]["price_gross"] for f in fr]
        mean = sum(pr) / len(pr)
        for e in tl:
            s = iso(e["start"]).astimezone(W)
            if s.day == day and e["role"] == "peak":
                assert e["price"] > mean


def _charges(tl):
    return [e for e in tl if e["role"] == "charge"]


@pytest.mark.parametrize("k,soc", [(2, 30), (4, 45)])
def test_user_scenario_greedy_night_charge_at_2300(k, soc):
    """22:05, real profile x k: tonight's cheapest hour (23:00) is the first grid charge, a need-sized block of that
    hour at the Reg 108 ceiling; the battery stays >= Min SOC; every charge hour is an hour of plan_charge_hours."""
    r = plan(NOW, soc, prof(k))
    ch = _charges(r["plan_timeline"])
    h23 = datetime(2026, 10, 22, 23, 0, tzinfo=W).astimezone(timezone.utc)
    assert ch and h23 <= iso(ch[0]["start"]) < h23 + timedelta(hours=1)
    assert iso(ch[0]["end"]) - iso(ch[0]["start"]) <= timedelta(hours=1) and ch[0]["charge_a"] == 100
    for e in r["plan_timeline"]:
        assert e["soc_end"] >= 20 - 1e-6, e


def test_higher_soc_never_needs_more_charge_hours():
    hours = lambda soc: {e["start"] for e in _charges(plan(NOW, soc, prof(2), terminal_fill=False)["plan_timeline"])}
    assert hours(90) <= hours(60) <= hours(30)


def test_no_profile_fills_to_max_soc_in_the_cheapest_hours():
    r = plan(NOW, 30, None)
    assert not [e for e in r["plan_timeline"] if e["role"] == "topup"]
    assert _charges(r["plan_timeline"])[-1]["soc_end"] >= 90 - 1e-6
    assert len(r["deye_tou_schedule"]) == 6
