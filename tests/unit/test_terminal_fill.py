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


def test_live_fixture_saturday_block_ends_at_the_cheap_valley_not_in_the_dearer_hour():
    res = pstryk_engine.plan_rolling_tou(LIVE_FRAMES, _at(19, 8), 17, PROFILE, **P)
    sat = (_at(0) + timedelta(days=1)).date()
    ch = [(pstryk_engine._iso_dt(e["start"]).astimezone(pstryk_engine.WARSAW_TZ),
           pstryk_engine._iso_dt(e["end"]).astimezone(pstryk_engine.WARSAW_TZ))
          for e in res["plan_timeline"] if e["role"] == "charge"]
    midday = [c for c in ch if c[0].date() == sat and 11 <= c[0].hour < 16]
    assert midday and min(c[0] for c in midday).strftime("%H:%M") == "11:10"
    assert max(c[1] for c in midday).strftime("%H:%M") == "14:00"


def test_hour_after_reaching_max_in_the_valley_is_never_charged():
    start = _at(8)
    prices = [0.30, 0.30, 0.30, 0.30, 0.90, 0.95, 1.00, 1.00]
    hours = []
    for n, p in enumerate(prices):
        a = start + timedelta(hours=n)
        hours.append((a, a + timedelta(hours=1), p, {"metrics": {"pricing": {"price_gross": p}}}))
    load = {h[0]: 3.0 for h in hours}
    picks = _plan(hours, start, 20, load)
    assert picks
    assert all(p["start"] < hours[4][0] for p in picks), [p["start"] for p in picks]


def _held_of(picks):
    return {p["start"].replace(minute=0, second=0, microsecond=0): (p["start"], p["end"]) for p in picks}


def _held_replan(soc_delta):
    hours = _hours(LIVE_FRAMES)
    now = _at(19, 8)
    base = _plan(hours, now, 17, {})
    return base, _plan(hours, now, 17 + soc_delta, {}, held=_held_of(base))


def test_held_block_shrinks_when_soc_clearly_higher():
    base, again = _held_replan(10)
    mins = lambda ps: sum((p["end"] - p["start"]).total_seconds() for p in ps)
    assert mins(again) < mins(base) - 60, "10 % more SOC must shorten the held charge, not keep it"
    assert [p["end"] for p in again][-1] == [p["end"] for p in base][-1]  # still ends where the cheap run ends


def test_held_block_kept_when_soc_drift_within_one_step():
    base, again = _held_replan(2)
    assert [(p["start"], p["end"]) for p in again] == [(p["start"], p["end"]) for p in base]
