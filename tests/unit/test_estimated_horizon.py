"""
TDD (plan /.claude/plans/try-to-continue-this-zippy-cloud.md section 1): estimated horizon.

ASSUMED API (config/pstryk_engine.py):
  plan_rolling_tou(..., estimate_beyond_horizon: bool = True)
    True : after the last published hour, one more day is appended whose hours copy the price of the same Warsaw
           hour of the last published day; load from the profile (weekday/weekend by date). Those hours only create
           shortfalls (they justify charging in published hours); they are never charge/topup hours and never a
           grid-charge slot of deye_tou_schedule (24 h from now).
    False: today's behaviour (planning ends at the last published hour).
  plan_timeline entries of estimated hours carry "estimated": True (absent/False otherwise); "horizon_end" stays the
  last PUBLISHED hour end.
  Stored-energy value: the SOC walk tracks the kWh-weighted cost of the battery energy (charge price*(1+loss);
  initial energy valued at cheapest published price*(1+loss)); an hour drains the battery (role drain/peak) only if
  its price > that cost, otherwise it holds.
Expectations are computed from fixture data (no time literals in assertions); live_2026_10_23.json = real frames
Fri 23.10 00:00 -> Sat 24.10 24:00 Warsaw, profile = deye/real_profile.json, now = Fri 12:18, SOC 17.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config"))
FIX = os.path.abspath(os.path.join(os.path.dirname(__file__), "../fixtures"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
import pstryk_engine  # noqa: E402

W = pstryk_engine.WARSAW_TZ
HOUR = timedelta(hours=1)
P = dict(min_soc=10, max_soc=90, capacity_kwh=16.13, voltage=51.2, ceiling_a=100.0, loss_pct=10.0)
LOSS = P["loss_pct"] / 100.0
CAP = P["capacity_kwh"]
MAX_KWH = P["ceiling_a"] * P["voltage"] / 1000.0 * (1 - LOSS)
GRID_ROLES = ("charge", "topup")


def _load(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as f:
        return json.load(f)


PROFILE = _load("deye", "real_profile.json")
FX = _load("pstryk", "live_2026_10_23.json")
FRAMES = FX["pricing_frames_today"] + FX["pricing_frames_tomorrow"]
NOW, SOC = datetime(2026, 10, 23, 12, 18, tzinfo=W), 17
HOURS = [(pstryk_engine._iso_dt(f["start"]), pstryk_engine._iso_dt(f["end"]), pstryk_engine.retail_gross_price(f), f)
         for f in FRAMES]
HORIZON_END = HOURS[-1][1]
DAY = timedelta(hours=24)


def _plan(est):
    return pstryk_engine.plan_rolling_tou(FRAMES, NOW, SOC, PROFILE, estimate_beyond_horizon=est, **P)


def _dt(s):
    return pstryk_engine._iso_dt(s)


def _grid_hours(plan, estimated=False):
    out = set()
    for e in plan["plan_timeline"]:
        if e["role"] in GRID_ROLES and bool(e.get("estimated")) == estimated:
            t = _dt(e["start"]).replace(minute=0, second=0, microsecond=0)
            while t < _dt(e["end"]):
                out.add(t)
                t += HOUR
    return out


def _loads(a, b):
    return {fc["start"]: fc["kwh"] for fc in pstryk_engine.load_forecast(a, b, PROFILE)}


def _est_hours():
    """Reference estimated day: same Warsaw clock hour of the last published day."""
    last = {h[0].astimezone(W).hour: h[2] for h in HOURS[-24:]}
    # keyed by the Warsaw clock hour of the estimated hour itself (differs from a plain +24 h copy across DST)
    return [(h[0] + DAY, h[1] + DAY, last[(h[0] + DAY).astimezone(W).hour], None) for h in HOURS[-24:]]


def _walk_deficit(charge_hours, hours):
    """Deficit kWh per hour start with the engine's own walk (worth-covering hours drain, charge hours fill)."""
    cand = [h for h in hours if h[1] > NOW]
    cheapest = min(h[2] for h in HOURS)
    worth = [h[2] > cheapest * (1 + LOSS) for h in cand]
    charges = {i: MAX_KWH for i, h in enumerate(cand) if h[0] in charge_hours}
    res = pstryk_engine._soc_walk(cand, worth, charges, SOC, _loads(cand[0][0], cand[-1][1]), min_soc=P["min_soc"],
                                  max_soc=P["max_soc"], capacity_kwh=CAP)
    return {h[0]: r[1] for h, r in zip(cand, res)}


def test_estimated_day_is_appended_flagged_and_priced_from_last_day():
    plan = _plan(True)
    est = [e for e in plan["plan_timeline"] if e.get("estimated")]
    assert est, "no estimated entries"
    assert _dt(plan["horizon_end"]) == HORIZON_END
    assert all(_dt(e["start"]) >= HORIZON_END for e in est)
    assert not any(e.get("estimated") for e in plan["plan_timeline"] if _dt(e["start"]) < HORIZON_END)
    assert _dt(est[-1]["end"]) == HORIZON_END + DAY
    ref = {h[0]: h[2] for h in _est_hours()}
    for e in est:  # entry price = price of its first hour
        assert ref[_dt(e["start"]).replace(minute=0)] == pytest.approx(e["price"], abs=1e-4)
        assert e["role"] not in GRID_ROLES


def test_estimated_hours_never_charge_or_become_grid_slots():
    plan = _plan(True)
    assert not _grid_hours(plan, estimated=True)
    # every clock hour a grid-charge slot spans is a published charge hour of the next 24 h (never estimated)
    published = {t.astimezone(W).hour for t in _grid_hours(plan) if t < NOW + DAY}
    slots = plan["deye_tou_schedule"]
    for i, s in enumerate(slots):
        if s["grid_charge"]:
            a = int(s["time"][:2]) * 60 + int(s["time"][3:5])
            nxt = slots[(i + 1) % len(slots)]["time"]
            b = int(nxt[:2]) * 60 + int(nxt[3:5])
            span = range(a, b if b > a else b + 24 * 60, 60)
            assert {(m // 60) % 24 for m in span} <= published, (s, sorted(published))


def test_estimated_shortfall_adds_published_charge_hours():
    base, ext = _grid_hours(_plan(False)), _grid_hours(_plan(True))
    assert base < ext, "estimate must add at least one published charge hour"
    # the justification: without the extension the estimated day has an uncovered shortfall
    hs = HOURS + _est_hours()
    assert sum(v for t, v in _walk_deficit(base, hs).items() if t >= HORIZON_END) > 0
    # cheapest published Saturday hours are already charged; the next-cheapest not-yet-charged one joins
    sat = [h for h in HOURS if h[0] >= HORIZON_END - DAY and h[1] > NOW]
    nxt = min((h for h in sat if h[0] not in base), key=lambda h: h[2])
    assert nxt[0] in ext
    # with the extension the estimated-day deficit is no worse
    assert sum(v for t, v in _walk_deficit(ext, hs).items() if t >= HORIZON_END) <= \
        sum(v for t, v in _walk_deficit(base, hs).items() if t >= HORIZON_END) + 1e-6


def test_false_flag_keeps_published_only_plan():
    plan = _plan(False)
    assert not any(e.get("estimated") for e in plan["plan_timeline"])
    assert _dt(plan["plan_timeline"][-1]["end"]) <= HORIZON_END

