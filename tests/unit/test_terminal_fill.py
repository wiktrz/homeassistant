"""
TDD: terminal fill (plan /.claude/plans/try-to-continue-this-zippy-cloud.md, sections 1-2).
After the shortfall charges, plan_charge_hours serves one more demand - Max SOC at the end of the published prices -
with cheapest-first hours whose price * (1 + loss%) <= the mean published price of their own Warsaw day.
Expectations are derived from fixture data (no time literals).
"""
import math
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))
from test_cheapest_hours import P, PROFILE, _at, _hours, _load, _load_by_hour, pstryk_engine  # noqa: E402

LIVE = _load("pstryk", "live_2026_10_23.json")
LIVE_FRAMES = LIVE["pricing_frames_today"] + LIVE["pricing_frames_tomorrow"]
LOSS = 1.0 + P["loss_pct"] / 100.0


def _day_mean(hours):
    by = {}
    for h in hours:
        by.setdefault(h[0].astimezone(pstryk_engine.WARSAW_TZ).date(), []).append(h[2])
    return {d: math.fsum(v) / len(v) for d, v in by.items()}


def _plan(hours, now, soc, load, **kw):
    return pstryk_engine.plan_charge_hours(hours, now, soc, load, **{**P, **kw})


def test_no_load_fills_cheap_hours_to_max_soc_only_below_day_mean():
    hours = _hours(LIVE_FRAMES)
    now = _at(19, 8)
    picks = _plan(hours, now, 17, {})
    assert picks, "no shortfall at all, yet the cheap valley must fill towards Max SOC"
    mean = _day_mean(hours)
    for p in picks:
        assert p["price"] * LOSS <= mean[p["start"].astimezone(pstryk_engine.WARSAW_TZ).date()] + 1e-9
    stored = math.fsum(p["energy_kwh"] for p in picks)
    assert stored <= (P["max_soc"] - 17) / 100.0 * P["capacity_kwh"] + 1e-6


def test_fill_off_keeps_need_sized_plan_and_fill_only_adds():
    hours = _hours(LIVE_FRAMES)
    now = _at(19, 8)
    load = _load_by_hour([h for h in hours if h[1] > now])
    base = {p["start"].replace(minute=0): p["energy_kwh"] for p in _plan(hours, now, 17, load, terminal_fill=False)}
    full = {p["start"].replace(minute=0): p["energy_kwh"] for p in _plan(hours, now, 17, load)}
    for k, e in base.items():  # shortfall charges keep priority: never shrink
        assert full.get(k, 0.0) >= e - 1e-6
    assert math.fsum(full.values()) > math.fsum(base.values())


def test_live_fixture_saturday_midday_charges_more_and_timeline_shows_real_block():
    res = pstryk_engine.plan_rolling_tou(LIVE_FRAMES, _at(19, 8), 17, PROFILE, **P)
    tl = res["plan_timeline"]
    charges = [e for e in tl if e["role"] == "charge"]
    assert charges
    for e in charges:
        assert e["target_soc"] == round(e["soc_end"])
    tomorrow = (_at(0) + timedelta(days=1)).date()
    day = [e for e in charges if pstryk_engine._iso_dt(e["start"]).astimezone(pstryk_engine.WARSAW_TZ).date() == tomorrow]
    assert max(e["soc_end"] for e in day) > 51  # need-sized plan ended at 51 %
    # the table still targets Max SOC
    assert all(s["target_soc"] == P["max_soc"] for s in res["deye_tou_schedule"] if s["grid_charge"])


def test_ai_prompt_carries_the_terminal_fill_rule():
    import ai_tou_planner
    ctx = ai_tou_planner.build_context(LIVE_FRAMES, _at(19, 8), 17, PROFILE, None, **P)
    rules = ai_tou_planner.build_prompt(ctx)[0]["content"]
    assert "unknown" in rules and "mean price of their own day" in rules and str(P["loss_pct"]) in rules
