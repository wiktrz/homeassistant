"""
TDD: price-window charging (plan R1-R7, item 1).
  - find_charge_window(frames, now): one window per published Warsaw price day (seed + price-delta extension);
  - plan_rolling_tou charges that window to Max SOC (greedy shortfall only before it, no terminal fill on its day);
  - charge_current_plan: price-weighted Reg 108 currents keyed by UTC hour ISO;
  - check_and_apply_charge_current_modulation: map lookup with the R6 advance-write rule.
"""
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
import energy_defaults  # noqa: E402
import pstryk_engine  # noqa: E402

W = pstryk_engine.WARSAW_TZ
UTC = timezone.utc
HOUR = timedelta(hours=1)
CAP = energy_defaults.FALLBACK_BATTERY_CAPACITY_KWH
VOLTS = energy_defaults.FALLBACK_BATTERY_NOMINAL_VOLTAGE
CEIL = energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A
LOSS = energy_defaults.DEFAULT_LOSS_BUFFER_PCT
P = dict(min_soc=10, max_soc=100, capacity_kwh=CAP, voltage=VOLTS, ceiling_a=CEIL, loss_pct=LOSS)
# Sun 25.10.2026 (DST end, 25 h): the user's prices by Warsaw clock hour, the rest of the day is dear
SUN = {6: .5061, 7: .2799, 8: .2591, 9: .2443, 10: .2571, 11: .2592, 12: .2604, 13: .3577}
DEAR = 0.6


def _frames(day, price_of_local_hour):
    """Hourly frames of one Warsaw day (23/24/25 h): price_of_local_hour(clock hour) -> gross price."""
    t = datetime(day.year, day.month, day.day, tzinfo=W).astimezone(UTC)
    nxt = (datetime(day.year, day.month, day.day, tzinfo=W) + timedelta(hours=36)).date()
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=W).astimezone(UTC)
    out = []
    while t < end:
        out.append({"start": pstryk_engine._iso_z(t), "end": pstryk_engine._iso_z(t + HOUR),
                    "metrics": {"pricing": {"price_gross": price_of_local_hour(t.astimezone(W).hour)}}})
        t += HOUR
    return out


def _sun():
    return _frames(date(2026, 10, 25), lambda h: SUN.get(h, DEAR))


def _hhmm(dt):
    return dt.astimezone(W).strftime("%H:%M")


NOW_SUN = datetime.fromisoformat("2026-10-25T06:00:00+01:00")


# ---- find_charge_window -------------------------------------------------------------------------------------------

def test_sunday_window_is_07_to_13():
    wins = pstryk_engine.find_charge_window(_sun(), NOW_SUN)
    assert len(wins) == 1
    w = wins[0]
    assert (_hhmm(w["start"]), _hhmm(w["end"])) == ("07:00", "13:00")
    assert len(w["hours"]) == 6 and w["end"] - w["start"] == 6 * HOUR


def test_extension_delta_and_seed_come_from_env(monkeypatch):
    monkeypatch.setattr(energy_defaults, "CHARGE_WINDOW_EXTEND_PRICE_DELTA", 0.0)
    w = pstryk_engine.find_charge_window(_sun(), NOW_SUN)[0]
    assert (_hhmm(w["start"]), _hhmm(w["end"])) == ("09:00", "11:00")  # seed only: neighbours are dearer than its average
    monkeypatch.setattr(energy_defaults, "CHARGE_WINDOW_SEED_HOURS", 3)
    w = pstryk_engine.find_charge_window(_sun(), NOW_SUN)[0]
    assert len(w["hours"]) == 3 and _hhmm(w["start"]) == "08:00"


def test_equal_seed_blocks_pick_the_earliest(monkeypatch):
    monkeypatch.setattr(energy_defaults, "CHARGE_WINDOW_EXTEND_PRICE_DELTA", 0.0)
    prices = {3: .2, 4: .2, 15: .2, 16: .2}
    w = pstryk_engine.find_charge_window(_frames(date(2026, 6, 10), lambda h: prices.get(h, DEAR)), datetime(2026, 6, 10, tzinfo=W))[0]
    assert _hhmm(w["start"]) == "03:00"


def test_window_on_23_hour_dst_day_is_contiguous_on_utc():
    # 29.03.2026: 02:00 -> 03:00 is skipped; cheap local hours 01 and 03 are UTC-consecutive
    cheap = {1: .1, 3: .1}
    frames = _frames(date(2026, 3, 29), lambda h: cheap.get(h, DEAR))
    assert len(frames) == 23
    w = pstryk_engine.find_charge_window(frames, datetime(2026, 3, 29, tzinfo=W))[0]
    assert (_hhmm(w["start"]), _hhmm(w["end"])) == ("01:00", "04:00") and w["end"] - w["start"] == 2 * HOUR


def test_window_on_25_hour_dst_day_spans_both_02_hours():
    cheap = {2: .1}  # the repeated 02:00 hour exists twice (CEST then CET)
    frames = _frames(date(2026, 10, 25), lambda h: cheap.get(h, DEAR))
    assert len(frames) == 25
    w = pstryk_engine.find_charge_window(frames, datetime(2026, 10, 25, tzinfo=W))[0]
    assert len(w["hours"]) >= 2 and w["end"] - w["start"] == len(w["hours"]) * HOUR
    assert w["start"].astimezone(UTC) == datetime(2026, 10, 25, 0, tzinfo=UTC)  # 02:00 CEST


def test_no_window_on_unpublished_or_partial_day():
    frames = _sun()
    assert pstryk_engine.find_charge_window(frames[:10], NOW_SUN) == []  # partial day = not published
    assert pstryk_engine.find_charge_window([], NOW_SUN) == []


def test_one_window_per_published_day_and_midnight_adjacent():
    d0, d1 = date(2026, 6, 10), date(2026, 6, 11)
    late = {22: .1, 23: .1}
    early = {0: .1, 1: .1}
    frames = _frames(d0, lambda h: late.get(h, DEAR)) + _frames(d1, lambda h: early.get(h, DEAR))
    wins = pstryk_engine.find_charge_window(frames, datetime(2026, 6, 10, tzinfo=W))
    assert [(_hhmm(w["start"]), _hhmm(w["end"])) for w in wins] == [("22:00", "00:00"), ("00:00", "02:00")]
    assert wins[0]["end"] == wins[1]["start"]


# ---- charge_current_plan ------------------------------------------------------------------------------------------

def _window(frames, now=NOW_SUN):
    return pstryk_engine.find_charge_window(frames, now)[0]


def test_currents_weighted_by_price_and_cover_the_energy():
    w = _window(_sun())
    amps = pstryk_engine.charge_current_plan(w, 10, 100, CAP, VOLTS, LOSS, CEIL)
    by_local = {datetime.fromisoformat(k.replace("Z", "+00:00")).astimezone(W).hour: a for k, a in amps.items()}
    assert set(by_local) == {7, 8, 9, 10, 11, 12}
    for hr, want in {7: 35, 8: 56, 9: 71, 10: 58, 11: 56, 12: 55}.items():
        assert abs(by_local[hr] - want) <= 1, (hr, by_local)
    assert max(by_local, key=by_local.get) == 9 and min(by_local, key=by_local.get) == 7  # cheapest / dearest
    need = (100 - 10) / 100 * CAP / (1 - LOSS / 100) * (1 + energy_defaults.CHARGE_WINDOW_ENERGY_MARGIN_PCT / 100)
    assert abs(sum(amps.values()) * VOLTS / 1000.0 - need) < 0.1 * len(amps) * VOLTS / 1000.0
    assert all(k.endswith("Z") and k[14:] == "00:00Z" for k in amps)


def test_currents_for_a_lower_max_soc_are_lower():
    w = _window(_sun())
    hi = pstryk_engine.charge_current_plan(w, 10, 100, CAP, VOLTS, LOSS, CEIL)
    lo = pstryk_engine.charge_current_plan(w, 10, 90, CAP, VOLTS, LOSS, CEIL)
    assert all(lo[k] < hi[k] for k in hi)


def test_currents_floor_clamp_and_ceiling_saturation():
    w = _window(_sun())
    floor = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A))
    tiny = pstryk_engine.charge_current_plan(w, 99, 100, CAP, VOLTS, LOSS, CEIL)
    assert set(tiny.values()) == {floor}
    assert set(pstryk_engine.charge_current_plan(w, 100, 100, CAP, VOLTS, LOSS, CEIL).values()) == {floor}
    huge = pstryk_engine.charge_current_plan(w, 0, 100, CAP * 10, VOLTS, LOSS, CEIL)
    assert max(huge.values()) == int(round(CEIL)) and all(floor <= a <= CEIL for a in huge.values())


def test_running_window_uses_live_soc_over_remaining_hours():
    w = _window(_sun())
    now = datetime(2026, 10, 25, 10, 20, tzinfo=W)
    amps = pstryk_engine.charge_current_plan(w, 60, 100, CAP, VOLTS, LOSS, CEIL, now=now)
    hrs = sorted(datetime.fromisoformat(k.replace("Z", "+00:00")).astimezone(W).hour for k in amps)
    assert hrs == [10, 11, 12]  # the running hour and the rest; hours already over are gone


# ---- plan_rolling_tou ---------------------------------------------------------------------------------------------

def _plan(frames=None, now=NOW_SUN, soc=10, **kw):
    return pstryk_engine.plan_rolling_tou(frames or _sun(), now, soc, None, **dict(P, **kw))


def test_plan_charges_one_window_to_max_soc_and_exposes_the_api():
    plan = _plan()
    cw = plan["charge_windows"]
    assert len(cw) == 1 and cw[0]["start"].endswith("Z")
    assert (_hhmm(pstryk_engine._iso_dt(cw[0]["start"])), _hhmm(pstryk_engine._iso_dt(cw[0]["end"]))) == ("07:00", "13:00")
    slots = plan["deye_tou_schedule"]
    ch = [s for s in slots if s["grid_charge"]]
    assert len(ch) == 1 and ch[0]["time"] == "07:00" and ch[0]["target_soc"] == 100
    assert 84 not in {s["target_soc"] for s in slots}
    amps = plan["charge_current_plan"]
    assert len(amps) == 6 and max(amps.values()) <= int(round(CEIL))
    assert all(isinstance(k, str) and k.endswith("Z") for k in amps)


def test_plan_target_is_user_max_soc_and_100_only_in_calibration():
    ch = lambda plan: [s for s in plan["deye_tou_schedule"] if s["grid_charge"]]
    assert ch(_plan(max_soc=90))[0]["target_soc"] == 90
    assert ch(_plan(max_soc=90, calibration_active=True))[0]["target_soc"] == 100


def test_pre_window_shortfall_is_still_charged_at_the_ceiling():
    # dear night hours with load and an empty battery: a cheap pre-window hour covers them, at the configured ceiling
    prices = {**SUN, 1: .20}
    frames = _frames(date(2026, 10, 25), lambda h: prices.get(h, DEAR))
    prof = {k: [1.0] * 24 for k in ("weekday", "weekend")}
    now = datetime.fromisoformat("2026-10-25T00:30:00+02:00")
    plan = pstryk_engine.plan_rolling_tou(frames, now, 10, prof, **P)
    tl = plan["plan_timeline"]
    pre = [e for e in tl if e["role"] == "charge" and pstryk_engine._iso_dt(e["end"]) <= datetime(2026, 10, 25, 7, tzinfo=W)]
    assert pre and all(e["charge_a"] == int(round(CEIL)) for e in pre)
    assert not any(e["role"] == "charge" and datetime(2026, 10, 25, 13, tzinfo=W) <= pstryk_engine._iso_dt(e["start"]) for e in tl)
    assert any(e["role"] == "charge" and _hhmm(pstryk_engine._iso_dt(e["start"])) == "07:00" for e in tl)


def test_no_charge_after_the_window_on_a_published_day():
    prof = {k: [1.0] * 24 for k in ("weekday", "weekend")}
    now = datetime.fromisoformat("2026-10-25T14:00:00+01:00")
    plan = pstryk_engine.plan_rolling_tou(_sun(), now, 30, prof, **P)
    assert not any(e["role"] == "charge" for e in plan["plan_timeline"])  # window over, no terminal fill either
    assert plan["charge_current_plan"] == {}


def test_midnight_adjacent_windows_form_one_wrapping_table_slot():
    d0, d1 = date(2026, 6, 10), date(2026, 6, 11)
    late, early = {22: .1, 23: .1}, {0: .1, 1: .1}
    frames = _frames(d0, lambda h: late.get(h, DEAR)) + _frames(d1, lambda h: early.get(h, DEAR))
    now = datetime(2026, 6, 10, 20, 0, tzinfo=W)
    plan = pstryk_engine.plan_rolling_tou(frames, now, 10, None, **P)
    assert len(plan["charge_windows"]) == 2
    ch = [s for s in plan["deye_tou_schedule"] if s["grid_charge"]]
    assert len(ch) == 1 and ch[0]["time"] == "22:00"  # 22:00-02:00 is one table slot that wraps the clock circle
    assert len(plan["deye_tou_schedule"]) == 6


def test_no_window_means_the_legacy_greedy_plan():
    # three hours of one day = not a published day: no window, no map, plan still produced
    now = datetime(2026, 6, 10, 1, 0, tzinfo=W)
    frames = [{"start": pstryk_engine._iso_z(now + i * HOUR), "end": pstryk_engine._iso_z(now + (i + 1) * HOUR),
               "metrics": {"pricing": {"price_gross": p}}} for i, p in enumerate([.3, 1.0, 1.0])]
    plan = pstryk_engine.plan_rolling_tou(frames, now, 80, {k: [8.0] * 24 for k in ("weekday", "weekend")}, **P)
    assert plan["charge_windows"] == [] and plan["charge_current_plan"] == {}


# ---- check_and_apply_charge_current_modulation (R6) ---------------------------------------------------------------

def _mod(amps, now, slots=None):
    with mock.patch.object(pstryk_engine, "set_charge_current_with_fallback", return_value={"success": True}) as m:
        res = pstryk_engine.check_and_apply_charge_current_modulation(
            slots or [], charge_current_plan=amps, now=now)
    return m.call_args[0][2], res


def _key(h):
    return pstryk_engine._iso_z(datetime(2026, 10, 25, h, tzinfo=W))


AMPS = {_key(7): 35, _key(8): 56, _key(9): 71, _key(10): 58}


def test_modulation_writes_the_current_hour_value_mid_hour():
    assert _mod(AMPS, datetime(2026, 10, 25, 8, 20, tzinfo=W))[0] == 56


def test_modulation_writes_next_hour_in_advance_when_not_lower():
    interval = energy_defaults.TOU_SYNC_INTERVAL_MIN
    # 08:50 -> 09:00 within one sync interval, 71 >= 56: written early
    assert _mod(AMPS, datetime(2026, 10, 25, 8, 60 - interval + 1, tzinfo=W))[0] == 71
    # boundary further away than one interval: still the current hour
    assert _mod(AMPS, datetime(2026, 10, 25, 8, 60 - interval - 1, tzinfo=W))[0] == 56


def test_modulation_never_lowers_the_current_early():
    interval = energy_defaults.TOU_SYNC_INTERVAL_MIN
    # 09 (71) -> 10 (58): the running hour keeps 71 until the boundary has passed
    assert _mod(AMPS, datetime(2026, 10, 25, 9, 60 - interval + 1, tzinfo=W))[0] == 71
    assert _mod(AMPS, datetime(2026, 10, 25, 10, 1, tzinfo=W))[0] == 58


def test_modulation_outside_the_map_is_the_ceiling_and_clamped():
    assert _mod(AMPS, datetime(2026, 10, 25, 15, 0, tzinfo=W))[0] == int(round(CEIL))
    assert _mod({_key(7): 1}, datetime(2026, 10, 25, 7, 5, tzinfo=W))[0] == int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A))
    assert _mod({_key(7): 500}, datetime(2026, 10, 25, 7, 5, tzinfo=W))[0] == int(round(CEIL))
