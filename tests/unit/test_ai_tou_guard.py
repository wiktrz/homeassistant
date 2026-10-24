"""AI TOU guard: live 24.10 table rejected, correct table accepted, planner integration, CLI. Hermetic."""
import json
import os
import sys
from datetime import datetime
from unittest import mock

import pytest

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
import ai_tou_guard  # noqa: E402
import ai_tou_planner  # noqa: E402
import pstryk_engine  # noqa: E402

W = pstryk_engine.WARSAW_TZ


def S(time, soc, grid, cur=100):
    return {"time": time, "target_soc": soc, "grid_charge": grid, "charge_current_a": cur}


# Sun 25.10 (clocks back): window 07:00-13:00 Warsaw, Max 90, Min 10, now 06:00.
GOOD = [S("00:00", 10, False), S("04:00", 10, False), S("07:00", 90, True), S("13:00", 10, False),
        S("17:00", 10, False), S("20:00", 10, False)]
LIVE = [S("04:00", 10, False), S("08:25", 84, True), S("11:00", 80, False), S("13:00", 10, False),
        S("14:00", 10, False), S("15:00", 10, False)]
CTX = {
    "now": datetime(2026, 10, 25, 6, 0, tzinfo=W).isoformat(),
    "battery": {"min_soc": 10, "max_soc": 90, "charge_current_ceiling_a": 100},
    "table": {"slot_count": 6, "minute_step": 5, "soc_step": 5},
    "charge_windows": [{"start": datetime(2026, 10, 25, 7, 0, tzinfo=W).isoformat(),
                        "end": datetime(2026, 10, 25, 13, 0, tzinfo=W).isoformat()}],
    "active_slots": GOOD,
    "algorithm_reference": {"table": GOOD},
}


def test_correct_table_accepted():
    assert ai_tou_guard.validate(GOOD, CTX) == []


def test_live_table_rejected_with_window_and_soc_errors():
    ctx = dict(CTX, active_slots=LIVE)
    errs = ai_tou_guard.validate(LIVE, ctx)
    assert "charge_window_not_covered" in errs
    assert "slot_2_charge_target_not_max" in errs or "slot_2_soc_bounds" in errs


def _mut(i, **kw):
    t = [dict(s) for s in GOOD]
    t[i].update(kw)
    return t


@pytest.mark.parametrize("table,code", [
    (_mut(1, target_soc=10.0), "slot_2_soc_type"),
    (_mut(1, target_soc=True), "slot_2_soc_type"),
    (_mut(1, extra=1), "slot_2_keys"),
    (_mut(1, time="04:01"), "slot_2_time"),
    (_mut(2, target_soc=80), "slot_3_charge_target_not_max"),
    (_mut(1, grid_charge=1), "slot_2_grid_charge"),
    (GOOD[:-1], "slot_count"),
])
def test_bad_tables_rejected(table, code):
    assert code in ai_tou_guard.validate(table, CTX)


def test_charge_current_ignored():
    assert ai_tou_guard.validate(_mut(2, charge_current_a=7), CTX) == []


def test_changed_active_slot_rejected():
    ctx = dict(CTX, now=datetime(2026, 10, 25, 9, 0, tzinfo=W).isoformat(),
               active_slots=[dict(s, grid_charge=False, target_soc=10) if s["time"] == "07:00" else s for s in GOOD])
    assert "active_slot_changed" in ai_tou_guard.validate(GOOD, ctx)


def test_algorithm_topup_outside_window_allowed():
    algo = [S("00:00", 10, False), S("04:00", 90, True), S("05:00", 10, False), S("07:00", 90, True),
            S("13:00", 10, False), S("17:00", 10, False)]
    assert ai_tou_guard.validate(algo, dict(CTX, algorithm_reference={"table": algo}, active_slots=algo)) == []
    assert "grid_charge_outside_window" in ai_tou_guard.validate(algo, dict(CTX, active_slots=algo))


# ---- planner integration ----

def test_evaluate_rejects_live_table_with_invalid_reason():
    algo = {"deye_tou_schedule": GOOD, "charge_current_plan": {}}
    ev = ai_tou_planner.evaluate_ai_slots(LIVE, algo, dict(CTX, active_slots=LIVE), [], datetime(2026, 10, 25, 6, tzinfo=W),
                                          10, {}, {})
    assert ev["ok"] is False and ev["reason"].startswith("invalid_")


def test_nan_cost_rejected():
    algo = {"deye_tou_schedule": GOOD, "charge_current_plan": {}}
    with mock.patch.object(ai_tou_planner, "simulate_table", return_value=(float("nan"), [])):
        ev = ai_tou_planner.evaluate_ai_slots(GOOD, algo, CTX, [], datetime(2026, 10, 25, 6, tzinfo=W), 10, {}, {})
    assert ev == {"ok": False, "reason": "invalid_cost_not_finite"}


def test_validate_ai_slots_stays_bounds_only_for_manual_editor():
    ok, _ = ai_tou_planner.validate_ai_slots(LIVE, dict(CTX))
    assert ok is True  # outside the window and SOC 84 < Max: the manual editor stays exempt


# ---- CLI ----

def _files(tmp_path, slots):
    r, c = tmp_path / "r.json", tmp_path / "c.json"
    r.write_text(json.dumps({"slots": slots}))
    c.write_text(json.dumps(dict(CTX, active_slots=slots)))
    return ["--response", str(r), "--context", str(c)]


def test_cli_exit_codes(tmp_path, capsys):
    assert ai_tou_guard.main(_files(tmp_path, GOOD)) == 0
    assert ai_tou_guard.main(_files(tmp_path, LIVE)) == 1
    assert "charge_window_not_covered" in capsys.readouterr().out
