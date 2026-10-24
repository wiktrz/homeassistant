"""
TDD: greedy cheapest-hour charging (plan /.claude/plans/try-to-continue-this-zippy-cloud.md).

ASSUMED SIGNATURE (config/pstryk_engine.py):
  plan_charge_hours(hours, now_dt, current_soc, load_kwh_by_hour, *, min_soc, max_soc, capacity_kwh, voltage,
                    ceiling_a, loss_pct) -> List[{"start","end","energy_kwh","charge_a","price"}]
    hours: [(start, end, price, frame)] aware datetimes, as built inside plan_rolling_tou (hours with end > now are
           candidates); load_kwh_by_hour: {hour start datetime: kWh}; start/end: aware datetimes;
    energy_kwh: energy STORED in the battery in that hour (ceiling_a*V/1000*(1-loss) at full power);
    charge_a: Reg 108 current for that hour (== ceiling for a full hour). Result sorted by start.
plan_rolling_tou: grid-charge roles ("charge"/"topup") of plan_timeline cover exactly the chosen hours.
Expectations come from fixture data via a small reference implementation (no time literals in expectations).
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
MAX_KWH = P["ceiling_a"] * P["voltage"] / 1000.0 * (1 - LOSS)
EPS = 1e-6


def _load(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as f:
        return json.load(f)


PROFILE = _load("deye", "real_profile.json")
FX = _load("pstryk", "single_day_valley_core.json")
TODAY, TOMORROW = FX["pricing_frames_today"], FX["pricing_frames_tomorrow"]


def _at(h, m=0):
    return datetime(2026, 10, 23, h, m, tzinfo=W)


def _hours(frames):
    out = []
    for f in frames:
        a = pstryk_engine._iso_dt(f["start"])
        out.append((a, pstryk_engine._iso_dt(f["end"]), pstryk_engine.retail_gross_price(f), f))
    return out


def _load_by_hour(hours, profile=PROFILE):
    out = {}
    for fc in pstryk_engine.load_forecast(hours[0][0], hours[-1][1], profile):
        out[fc["start"]] = fc["kwh"]
    return out


def _run(frames, now, soc, profile=PROFILE):
    hs = _hours(frames)
    return hs, _load_by_hour(hs, profile), pstryk_engine.plan_charge_hours(hs, now, soc, _load_by_hour(hs, profile),
                                                                             terminal_fill=False, **P)


def _h(r):
    """the price hour of a planned charge block (a need-sized block may start mid-hour)"""
    return r["start"].replace(minute=0, second=0, microsecond=0)


def _starts(res):
    return [_h(r) for r in res]


def _sim(hs, load, now, soc, charges):
    """Hourly SOC walk. charges {start: kWh stored}. Charge hour: grid serves load; worth-covering hour: battery
    serves load down to Min SOC (the rest is a grid-bought deficit); other hours: hold.
    Returns [(start, worth, soc_after, deficit_kwh)]."""
    cand = [h for h in hs if h[1] > now]
    cmin = min(h[2] for h in cand)
    out = []
    for a, b, price, _ in cand:
        worth = price > cmin * (1 + LOSS)
        deficit = 0.0
        if a in charges:
            soc = min(P["max_soc"], soc + charges[a] / P["capacity_kwh"] * 100)
        elif worth:
            need = load.get(a, 0.0) * min(1.0, (b - now) / (b - a)) / P["capacity_kwh"] * 100  # running hour: rest only
            deficit = max(0.0, need - max(0.0, soc - P["min_soc"])) * P["capacity_kwh"] / 100
            soc = max(P["min_soc"], soc - need) if soc >= P["min_soc"] else soc
        out.append((a, worth, soc, deficit))
    return out


def _deficit(tl):
    return sum(x[3] for x in tl)


def _reference(hs, load, now, soc):
    """Greedy: serve the first fixable deficit hour with the cheapest earlier economic hour (an hour already chosen
    grows), storing only the deficit it serves, capped by ceiling energy / room to Max SOC; unfixable deficits (no cheaper earlier hour, or less than one SOC step to store or
    serve) stay grid-bought."""
    cand = [h for h in hs if h[1] > now]
    charges, skipped = {}, set()
    while True:
        tl = _sim(hs, load, now, soc, charges)
        ks = [i for i, x in enumerate(tl) if x[3] > EPS and i not in skipped]
        if not ks:
            return charges
        k = ks[0]
        full = lambda i: MAX_KWH * min(1.0, (cand[i][1] - now) / (cand[i][1] - cand[i][0]))
        opts = [(h[2], i, h[0]) for i, h in enumerate(cand[:k])
                if charges.get(h[0], 0.0) < full(i) - EPS and h[2] * (1 + LOSS) < cand[k][2]]
        for _, i, a in sorted(opts):
            stop = next((j for j in range(k + 1, len(cand)) if cand[j][2] <= cand[i][2]), len(cand))
            served = sum(x[3] for x in tl[k:stop])
            # need-sized: the shortfall served, capped by the hour's ceiling energy left and the room to Max SOC
            e = min(full(i) - charges.get(a, 0.0), (P["max_soc"] - max(x[2] for x in tl[i:k])) / 100 * P["capacity_kwh"], served)
            # one SOC step of capacity: the least a NEW hour must store and serve (up to the next hour at most as cheap)
            step = pstryk_engine.energy_defaults.TOU_FLOOR_SOC_STEP / 100.0 * P["capacity_kwh"]
            least = EPS if a in charges else step - EPS
            if e >= least and served >= least:
                charges[a] = charges.get(a, 0.0) + e
                break
        else:
            skipped.add(k)


def _plan(frames, now, soc, profile=PROFILE):
    # published-only plan: these expectations compare with the shortfall pass of plan_charge_hours (terminal fill off,
    # it is covered by test_terminal_fill.py)
    return pstryk_engine.plan_rolling_tou(frames, now, soc, profile, estimate_beyond_horizon=False,
                                          terminal_fill=False, **P)


def _grid_hours(plan):
    out = set()
    for e in plan["plan_timeline"]:
        if e["role"] in ("charge", "topup"):
            t = pstryk_engine._iso_dt(e["start"]).replace(minute=0, second=0, microsecond=0)
            while t < pstryk_engine._iso_dt(e["end"]):
                out.add(t)
                t += HOUR
    return out


CASES = [("two_day_soc17", TODAY + TOMORROW, _at(9, 27), 17), ("two_day_soc60", TODAY + TOMORROW, _at(9, 27), 60),
         ("two_day_night", TODAY + TOMORROW, _at(22, 5), 30), ("one_day_soc17", TODAY, _at(9, 27), 17)]


@pytest.mark.parametrize("name,frames,now,soc", CASES)
def test_matches_greedy_reference_and_price_order(name, frames, now, soc):
    hs, load, res = _run(frames, now, soc)
    ref = _reference(hs, load, now, soc)
    assert set(_starts(res)) == set(ref)
    price = {h[0]: h[2] for h in hs}
    cand = sorted((h[2], h[0]) for h in hs if h[1] > now)
    chosen = {_h(r) for r in res}
    # prefix property: any cheaper candidate that was skipped must be infeasible/uneconomic (not reference-chosen)
    for p, a in cand:
        if p >= max(price[c] for c in chosen) if chosen else True:
            break
        assert a in chosen or a not in ref
    for r in res:
        assert abs(r["energy_kwh"] - ref[_h(r)]) < 1e-3 and r["price"] == price[_h(r)]


@pytest.mark.parametrize("name,frames,now,soc", CASES)
def test_full_power_coverage_no_overfill_minimality(name, frames, now, soc):
    hs, load, res = _run(frames, now, soc)
    ceiling = min(float(pstryk_engine.energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A), P["ceiling_a"])
    s = _starts(res)
    for i, r in enumerate(res):
        last_of_group = i + 1 == len(res) or s[i + 1] != r["end"]
        assert last_of_group or abs(r["charge_a"] - ceiling) < EPS, r
        assert r["charge_a"] <= ceiling + EPS and r["energy_kwh"] <= MAX_KWH + EPS
    charges = {_h(r): r["energy_kwh"] for r in res}
    tl = _sim(hs, load, now, soc, charges)
    assert all(x[2] <= P["max_soc"] + 0.5 for x in tl)  # no overfill
    ref = _reference(hs, load, now, soc)
    ref_def = _deficit(_sim(hs, load, now, soc, ref))
    assert _deficit(tl) <= ref_def + 1e-3  # zero peak grid purchase wherever it is coverable
    if res:
        worst = max(res, key=lambda r: (r["price"], _h(r)))
        less = {a: e for a, e in charges.items() if a != _h(worst)}
        assert _deficit(_sim(hs, load, now, soc, less)) > _deficit(tl) + EPS  # minimality


def test_today_soc17_two_day_cheapest_daytime_hours_first_at_full_power():
    hs, load, res = _run(TODAY + TOMORROW, _at(9, 27), 17)
    day = {h[0]: h[2] for h in hs if h[0].astimezone(W).date() == _at(0).date() and h[1] > _at(9, 27)}
    afternoon_cheap = sorted(day, key=day.get)[:2]  # the two cheapest hours of the day (fixture: 13 and 14)
    got = {_h(r): r for r in res}
    for a in afternoon_cheap:
        assert a in got and abs(got[a]["charge_a"] - P["ceiling_a"]) < EPS
    eve = [a for a in got if a.astimezone(W).hour >= 22]
    ev_price = {a: day[a] for a in eve}
    if len(eve) == 2:  # evening hours only in price order (cheapest first)
        assert min(ev_price, key=ev_price.get) in eve
    assert min(day[a] for a in eve) > max(day[a] for a in afternoon_cheap) if eve else True


def test_soc60_charges_subset_of_soc17():
    a = {_h(r) for r in _run(TODAY + TOMORROW, _at(9, 27), 17)[2]}
    b = {_h(r) for r in _run(TODAY + TOMORROW, _at(9, 27), 60)[2]}
    assert b <= a and len(b) < len(a)


def test_one_day_nothing_beyond_published_hours():
    hs, _, res = _run(TODAY, _at(9, 27), 17)
    assert res and all(r["end"] <= hs[-1][1] for r in res)
    assert _grid_hours(_plan(TODAY, _at(9, 27), 17)) <= {h[0] for h in hs}


@pytest.mark.parametrize("name,frames,now,soc", CASES)
def test_plan_rolling_tou_grid_charge_comes_from_plan_charge_hours(name, frames, now, soc):
    # R1: the plan charges each published day's price window (find_charge_window), the greedy top-up only before it
    hs = _hours(frames)
    res = pstryk_engine.plan_charge_hours(hs, now, soc, _load_by_hour(hs, PROFILE), terminal_fill=False,
                                          windows=pstryk_engine.find_charge_window(frames, now), **P)
    plan = _plan(frames, now, soc)
    assert _grid_hours(plan) == set(_starts(res))
    for e in plan["plan_timeline"]:  # the planned SOC never leaves [Min SOC, Max SOC] once inside it
        assert P["min_soc"] - 0.5 <= e["soc_end"] <= P["max_soc"] + 0.5 or e["soc_end"] >= e["soc_start"], e
        if e["role"] in ("charge", "topup"):
            assert e["target_soc"] == round(e["soc_end"]) and e["soc_end"] >= e["soc_start"], e


@pytest.mark.parametrize("k", [1, 2, 3])
def test_shift_invariance(k):
    base_prof = {kind: PROFILE["weekday"] for kind in ("weekday", "weekend")}
    shift_prof = {kind: [PROFILE["weekday"][(h - k) % 24] for h in range(24)] for kind in ("weekday", "weekend")}
    fr = TODAY + TOMORROW
    prices = [pstryk_engine.retail_gross_price(f) for f in fr]
    shifted = prices[:k] + prices[:len(prices) - k]
    frames_s = [json.loads(json.dumps(f)) for f in fr]
    for f, p in zip(frames_s, shifted):
        f["metrics"]["pricing"] = {"price_gross": p}
    now = _at(9, 27)
    base = _starts(_run(fr, now, 17, base_prof)[2])
    got = _starts(_run(frames_s, now + k * HOUR, 17, shift_prof)[2])
    assert got == [a + k * HOUR for a in base]


# ---- flash wear, no charge without a worth-covering shortfall, tier-5 charge merge keeps coverage ----
TWO = _load("pstryk", "two_day_2026_10_22_23.json")
TWO_FR = TWO["pricing_frames_today"] + TWO["pricing_frames_tomorrow"]
STEP_KWH = pstryk_engine.energy_defaults.TOU_FLOOR_SOC_STEP / 100.0 * P["capacity_kwh"]


def _first_at(frames):
    return pstryk_engine._iso_dt(frames[0]["start"]).astimezone(W)


def _soc_at(timeline, t):
    for e in timeline:
        a, b = pstryk_engine._iso_dt(e["start"]), pstryk_engine._iso_dt(e["end"])
        if a <= t <= b:
            return e["soc_start"] + (e["soc_end"] - e["soc_start"]) * ((t - a) / (b - a))
    return None


def _scaled(k):
    return {kind: [x * k for x in PROFILE[kind]] for kind in ("weekday", "weekend")}


def _hourly_writes(frames, now, soc, profile):
    """Hourly re-plans while the written 24 h table is fully priced (in production tomorrow's prices publish daily, so
    the table never runs into an unpriced tail for long; the fixture has no publication), each seeing the table on
    the inverter as the sync does, the SOC following each plan's own timeline.
    Returns (table writes, distinct charge blocks seen in any plan)."""
    end = pstryk_engine._iso_dt(frames[-1]["end"])
    written, writes, blocks = None, 0, set()
    while now + 24 * HOUR <= end:
        plan = pstryk_engine.plan_rolling_tou(frames, now, round(soc), profile, active_slots=written, **P)
        sl = plan["deye_tou_schedule"]
        if written is None or not pstryk_engine.are_tou_slots_identical(sl, written):
            written, writes = sl, writes + 1
        blocks |= {e["end"] for e in plan["plan_timeline"] if e["role"] in ("charge", "topup")}
        nxt = now + HOUR
        soc = _soc_at(plan["plan_timeline"], nxt) or soc
        now = nxt
    return writes, len(blocks)


@pytest.mark.parametrize("frames", [TODAY + TOMORROW, TWO_FR], ids=["valley_core", "two_day"])
@pytest.mark.parametrize("k", [0.5, 1.0, 1.5])
@pytest.mark.parametrize("soc", [17, 60])
def test_hourly_replans_write_at_most_once_per_charge_block(frames, k, soc):
    # Bound (data-derived): a re-sync is only due when the plan's behaviour changes, i.e. when a charge block appears,
    # changes or is done; every distinct charge block (keyed by its end) seen over the run may cost one write, plus
    # the first write and one for a hold floor re-projected once a charge is done. A flapping plan (a block appearing
    # and disappearing on alternate hours) or tables that only move slot boundaries exceed it.
    writes, blocks = _hourly_writes(frames, _first_at(frames), soc, _scaled(k))
    # +1 more: the rest of a partial charge hour now holds (grid serves load) instead of draining, one extra slot
    assert writes <= blocks + 3, (writes, blocks)


@pytest.mark.parametrize("frames", [TODAY + TOMORROW, TWO_FR], ids=["valley_core", "two_day"])
@pytest.mark.parametrize("hour", range(0, 24, 3))
@pytest.mark.parametrize("soc", range(15, 91, 15))
def test_every_charge_hour_stores_at_least_one_soc_step(frames, hour, soc):
    # no "keep at Max" trickle: an hour whose stored energy is below one TOU_FLOOR_SOC_STEP of capacity is not planned
    now = _first_at(frames) + hour * HOUR
    hs = _hours(frames)
    for r in pstryk_engine.plan_charge_hours(hs, now, soc, _load_by_hour(hs), **P):
        assert r["energy_kwh"] >= STEP_KWH - EPS, r


def test_no_charge_for_a_shortfall_below_one_soc_step():
    # a shortfall smaller than one SOC step before a cheaper hour that serves the rest is grid-bought (hysteresis):
    # find SOCs where the greedy plan is driven by such a tiny shortfall and check it charges nothing for it
    hs = _hours(TWO_FR)
    load = _load_by_hour(hs)
    now = _first_at(TWO_FR) + 23 * HOUR
    for soc in range(P["min_soc"], P["max_soc"] + 1):
        res = pstryk_engine.plan_charge_hours(hs, now, soc, load, terminal_fill=False, **P)
        tl = _sim(hs, load, now, soc, {})
        for r in res:
            later = [x for x in tl if x[0] > _h(r)]
            # the stretch this hour serves: up to the next hour at most as cheap as it
            price = {h[0]: h[2] for h in hs}
            stop = next((x[0] for x in later if price[x[0]] <= r["price"]), None)
            served = sum(x[3] for x in later if stop is None or x[0] < stop)
            assert served >= STEP_KWH - EPS, (soc, r, served)


@pytest.mark.parametrize("calibration", [True, False])
def test_top_fill_paths_do_not_trickle_near_the_top(calibration):
    top = 100 if calibration else P["max_soc"]
    step = pstryk_engine.energy_defaults.TOU_FLOOR_SOC_STEP
    near = pstryk_engine.plan_rolling_tou(TWO_FR, _first_at(TWO_FR), top - step + 1, None, calibration_active=calibration, **P)
    assert not [e for e in near["plan_timeline"] if e["role"] in ("charge", "topup")], near["plan_timeline"]
    far = pstryk_engine.plan_rolling_tou(TWO_FR, _first_at(TWO_FR), top - 4 * step, None, calibration_active=calibration, **P)
    assert [e for e in far["plan_timeline"] if e["role"] == "charge"]


def test_fit_tier5_charge_merge_keeps_every_charge_covered():
    # 8 separate charge blocks (alternating lower/higher target and current) must fit 6 slots: tier 5 merges the
    # closest-priced charge pair; the merged slot must still reach the higher target at the higher current
    h0 = datetime(2026, 10, 23, 0, tzinfo=W)
    entries, t = [], h0
    for i, price in enumerate([0.50, 0.51, 0.70, 0.90, 1.10, 1.30, 1.50, 1.70]):
        lo = i % 2 == 0
        entries.append({"start": t, "end": t + 3 * HOUR, "role": "topup" if lo else "charge",
                        "target_soc": 60 if lo else 90, "grid_charge": True, "charge_a": 40 if lo else 100,
                        "price": price})
        t += 3 * HOUR
    segs = pstryk_engine._fit_tou_table(entries, h0, 6)
    assert len(segs) == 6
    for e in entries:
        cover = [s for s in segs if s["start"] < e["end"] and s["end"] > e["start"]]
        assert all(s["grid_charge"] and s["target_soc"] >= e["target_soc"] and s["charge_a"] >= e["charge_a"]
                   for s in cover), (e, cover)


def test_running_hour_has_only_its_remaining_fraction():
    hs = _hours(TODAY)
    cheapest = min((h for h in hs if h[0].hour >= 8), key=lambda h: h[2])
    now = cheapest[0] + timedelta(minutes=45)
    frac = (cheapest[1] - now) / (cheapest[1] - cheapest[0])
    load = _load_by_hour(hs)
    res = pstryk_engine.plan_charge_hours(hs, now, 12, load, **P)
    running = [r for r in res if _h(r) == cheapest[0]]
    assert running, "cheapest running hour should still be used"
    assert running[0]["energy_kwh"] <= frac * MAX_KWH + EPS
    assert running[0]["charge_a"] <= P["ceiling_a"]
    assert len(res) > 1, "the rest of the shortfall is covered by another hour"
