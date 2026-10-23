"""
TDD (user decisions after plan section 1):
  1. plan_rolling_tou(..., estimate_beyond_horizon=False) by default.
  2. Minute-granular partial charging: a charge hour needing only part of an hour charges at the Reg 108 ceiling for
     ceil(minutes / TOU_SLOT_MINUTE_STEP) * step minutes, contiguous with an adjacent chosen charge hour, else at the
     end of its hour; a running hour from now.
"""
import inspect
import math
import os
import sys
from datetime import datetime, timedelta

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config"))
FIX = os.path.abspath(os.path.join(os.path.dirname(__file__), "../fixtures"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
import energy_defaults  # noqa: E402
import pstryk_engine  # noqa: E402

W = pstryk_engine.WARSAW_TZ
HOUR = timedelta(hours=1)
P = dict(min_soc=10, max_soc=90, capacity_kwh=16.13, voltage=51.2, ceiling_a=100.0, loss_pct=10.0)
CAP = P["capacity_kwh"]
MAX_KWH = P["ceiling_a"] * P["voltage"] / 1000.0 * (1 - P["loss_pct"] / 100.0)
STEP = int(energy_defaults.TOU_SLOT_MINUTE_STEP)
T0 = datetime(2026, 10, 23, 1, 0, tzinfo=W)


def _hours(prices):
    return [(T0 + i * HOUR, T0 + (i + 1) * HOUR, p, {}) for i, p in enumerate(prices)]


def _minutes(kwh):
    return math.ceil(kwh / MAX_KWH * 60.0 / STEP - 1e-9) * STEP


def test_estimate_beyond_horizon_is_off_by_default():
    assert inspect.signature(pstryk_engine.plan_rolling_tou).parameters["estimate_beyond_horizon"].default is False


def test_partial_hour_charges_at_ceiling_at_end_of_its_hour():
    hs = _hours([0.30, 1.00])
    soc = 80  # room to Max SOC: 10 % < one full hour
    res = pstryk_engine.plan_charge_hours(hs, T0, soc, {hs[1][0]: 15.0}, **P)
    assert len(res) == 1
    r = res[0]
    room = (P["max_soc"] - soc) / 100.0 * CAP
    assert r["charge_a"] == int(P["ceiling_a"])
    assert r["end"] == hs[0][1]
    assert r["end"] - r["start"] == timedelta(minutes=_minutes(room))
    assert abs(r["energy_kwh"] - room) < 1e-6


def test_partial_hour_is_contiguous_with_adjacent_charge_hour():
    hs = _hours([0.30, 0.31, 1.00])
    res = pstryk_engine.plan_charge_hours(hs, T0, 45, {hs[2][0]: 15.0}, **P)
    assert len(res) == 2
    full, part = res
    assert full["start"] == hs[0][0] and full["end"] == hs[0][1] and full["charge_a"] == int(P["ceiling_a"])
    assert part["start"] == full["end"], "partial block merges into the full charge hour"
    assert part["charge_a"] == int(P["ceiling_a"])
    assert part["end"] - part["start"] == timedelta(minutes=_minutes(part["energy_kwh"]))
    assert part["end"] < hs[1][1]


def test_partial_hour_before_chosen_hour_ends_at_its_start():
    hs = _hours([0.31, 0.30, 1.00])
    res = pstryk_engine.plan_charge_hours(hs, T0, 45, {hs[2][0]: 15.0}, **P)
    assert len(res) == 2
    part, full = res
    assert full["start"] == hs[1][0] and part["end"] == full["start"]
    assert part["start"] > hs[0][0]


def test_partial_running_hour_starts_now():
    hs = _hours([0.30, 1.00])
    now = T0 + timedelta(minutes=10)
    res = pstryk_engine.plan_charge_hours(hs, now, 80, {hs[1][0]: 15.0}, **P)
    assert len(res) == 1
    r = res[0]
    assert r["start"] <= now < r["start"] + timedelta(minutes=STEP)
    assert r["end"] - now >= timedelta(minutes=r["energy_kwh"] / MAX_KWH * 60.0) - timedelta(seconds=1)
    assert r["charge_a"] == int(P["ceiling_a"])


def test_rolling_plan_partial_charge_has_ceiling_current_and_minute_span():
    hs = _hours([0.30, 1.00, 1.00])
    frames = [{"start": pstryk_engine._iso_z(a), "end": pstryk_engine._iso_z(b),
               "metrics": {"pricing": {"price_gross": p}}} for a, b, p, _ in hs]
    prof = {k: [8.0] * 24 for k in ("weekday", "weekend")}
    plan = pstryk_engine.plan_rolling_tou(frames, T0, 80, prof, **P)
    ch = [e for e in plan["plan_timeline"] if e["role"] == "charge"]
    assert len(ch) == 1 and ch[0]["charge_a"] == int(P["ceiling_a"])
    a, b = pstryk_engine._iso_dt(ch[0]["start"]), pstryk_engine._iso_dt(ch[0]["end"])
    assert b == hs[0][1] and (b - a) < HOUR and (b - a).total_seconds() % (STEP * 60) == 0
    slots = [s for s in plan["deye_tou_schedule"] if s["grid_charge"]]
    assert slots and all(s["charge_current_a"] == int(P["ceiling_a"]) for s in slots)
    assert any(s["time"].endswith(f":{a.astimezone(W).minute:02d}") for s in slots)




def test_need_sized_charge_stores_only_the_shortfall_it_serves():
    # ~1 kWh shortfall in the dear hour: a short block at the Reg 108 ceiling, not a full hour
    hs = _hours([0.30, 1.00])
    need = 1.0
    res = pstryk_engine.plan_charge_hours(hs, T0, P["min_soc"], {hs[1][0]: need}, **P)
    assert len(res) == 1
    r = res[0]
    assert abs(r["energy_kwh"] - need) < 1e-6
    assert r["charge_a"] == int(P["ceiling_a"])
    assert r["end"] == hs[0][1] and r["end"] - r["start"] == timedelta(minutes=_minutes(need)) < HOUR


def test_need_sized_charge_tops_up_its_hour_for_a_later_shortfall():
    # two separated dear hours served by the same cheap hour: one block sized to both, no second charge hour
    hs = _hours([0.30, 0.50, 1.00, 0.30, 1.00])
    res = pstryk_engine.plan_charge_hours(hs, T0, P["min_soc"], {hs[2][0]: 1.0, hs[4][0]: 1.0}, **P)
    assert [r["start"].replace(minute=0) for r in res] == [hs[0][0]]
    assert abs(res[0]["energy_kwh"] - 2.0) < 1e-6


def _frames(hs):
    return [{"start": pstryk_engine._iso_z(a), "end": pstryk_engine._iso_z(b),
             "metrics": {"pricing": {"price_gross": p}}} for a, b, p, _ in hs]


def test_rest_of_partial_charge_hour_holds_so_peak_shortfall_is_covered():
    hs = _hours([0.30, 1.00, 1.00])
    prof = {k: [3.0] * 24 for k in ("weekday", "weekend")}
    plan = pstryk_engine.plan_rolling_tou(_frames(hs), T0, 30, prof, terminal_fill=False, **P)
    tl = plan["plan_timeline"]
    ch = [e for e in tl if e["role"] == "charge"]
    assert len(ch) == 1 and pstryk_engine._iso_dt(ch[0]["end"]) - pstryk_engine._iso_dt(ch[0]["start"]) < HOUR
    peak0 = pstryk_engine._iso_z(hs[1][0])
    at_peak = next(e for e in tl if e["start"] == peak0)
    shortfall_pct = 2 * 3.0 / CAP * 100.0  # two peak hours at 3 kW
    assert at_peak["soc_start"] >= min(P["max_soc"], P["min_soc"] + shortfall_pct) - 1.0
    pre = [e for e in tl if e["end"] <= pstryk_engine._iso_z(pstryk_engine._iso_dt(ch[0]["start"])) and e["role"] != "charge"]
    assert pre and all(e["role"] == "hold" for e in pre)


def test_fit_table_forced_keeps_all_planned_charge_minutes():
    segs, t = [], T0
    for i in range(8):  # 8 separated partial charge blocks (+ gaps) within 24 h
        segs.append({"start": t, "end": t + timedelta(minutes=30), "role": "charge", "grid_charge": True,
                     "target_soc": 90, "charge_a": 100, "price": 0.3 + i / 100})
        segs.append({"start": t + timedelta(minutes=30), "end": t + timedelta(minutes=180),
                     "role": "peak" if i % 2 else "hold", "grid_charge": False,
                     "target_soc": 10 if i % 2 else 50, "charge_a": 0, "price": 1.0})
        t += timedelta(minutes=180)
    out = pstryk_engine._fit_tou_table(segs, T0, 6)
    assert len(out) == 6
    n = len(out)
    mins = sum(((out[(i + 1) % n]["clock_m"] - out[i]["clock_m"]) % 1440 or 1440)
               for i in range(n) if out[i]["grid_charge"])
    assert mins >= 8 * 30
