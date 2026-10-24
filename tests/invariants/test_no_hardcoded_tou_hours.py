#!/usr/bin/env python3
"""
INVARIANT TEST: NO HARDCODED TOU HOURS OR CLOCK HEURISTICS
Enforces that battery Time-of-Use (TOU) schedules and register modulations
are strictly dynamic, driven exclusively by Pstryk dynamic tariff data,
and contain zero hardcoded clock heuristics, fixed event start hours, or
unverified PV refill assumptions.
"""

import ast
import os
import re
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

FORBIDDEN_HEURISTIC_CONSTANTS = {
    "MORNING_PEAK_START_HOUR",
    "PRE_PEAK_RESERVE_START_HOUR",
    "EVENING_PEAK_START_HOUR",
    "REG108_DAYTIME_START_HOUR",
    "REG108_EVENING_PRESET_HOUR",
}

FORBIDDEN_FIXED_HOURS = {5, 6, 10, 12, 17, 19}
FORBIDDEN_FIXED_MINUTES = {h * 60 for h in FORBIDDEN_FIXED_HOURS}  # {300, 360, 600, 720, 1020, 1140}
FORBIDDEN_EVENT_VARIABLE_NAMES = {"morning_m", "refill_m", "evening_peak_m", "evening_peak"}


def test_no_ai_invented_clock_heuristic_constants():
    """
    INVARIANT: No AI-invented clock heuristic constants.
    Ensures neither energy_defaults.py nor pstryk_engine.py defines or references
    variables/constants matching MORNING_PEAK_START_HOUR, PRE_PEAK_RESERVE_START_HOUR,
    EVENING_PEAK_START_HOUR, REG108_DAYTIME_START_HOUR, or REG108_EVENING_PRESET_HOUR.
    """
    files_to_check = [
        os.path.join(CONFIG_DIR, "energy_defaults.py"),
        os.path.join(CONFIG_DIR, "pstryk_engine.py"),
    ]

    violations = []

    for fpath in files_to_check:
        if not os.path.exists(fpath):
            continue
        rel_path = os.path.relpath(fpath, REPO_ROOT)
        with open(fpath, "r", encoding="utf-8") as f:
            src = f.read()

        tree = ast.parse(src, filename=fpath)

        for node in ast.walk(tree):
            # Check variable assignments
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in FORBIDDEN_HEURISTIC_CONSTANTS:
                        violations.append(
                            f"{rel_path}:{node.lineno} defines forbidden constant '{target.id}'"
                        )
            # Check attribute access (e.g. energy_defaults.MORNING_PEAK_START_HOUR)
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_HEURISTIC_CONSTANTS:
                violations.append(
                    f"{rel_path}:{node.lineno} references forbidden attribute '{node.attr}'"
                )
            # Check direct name reference
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_HEURISTIC_CONSTANTS:
                violations.append(
                    f"{rel_path}:{node.lineno} references forbidden name '{node.id}'"
                )

    assert not violations, (
        "AI-invented clock heuristic constants found in codebase:\n"
        + "\n".join(violations)
        + "\nAll TOU schedules must be dynamically derived from Pstryk price windows, not clock heuristics."
    )


def test_no_hardcoded_fixed_hour_events_in_tou_generation():
    """
    INVARIANT: Zero hardcoded fixed event start hours in TOU generation.
    Ensures generate_deye_tou_schedule in pstryk_engine.py does not hardcode
    fixed hour literals (e.g. 5, 6, 10, 17, 19) or their minute equivalents
    as event start boundaries.
    """
    pstryk_path = os.path.join(CONFIG_DIR, "pstryk_engine.py")
    assert os.path.exists(pstryk_path), f"Missing {pstryk_path}"

    with open(pstryk_path, "r", encoding="utf-8") as f:
        src = f.read()

    tree = ast.parse(src, filename=pstryk_path)
    rel_path = os.path.relpath(pstryk_path, REPO_ROOT)

    tou_func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "generate_deye_tou_schedule":
            tou_func_node = node
            break

    assert tou_func_node is not None, "generate_deye_tou_schedule function not found in pstryk_engine.py"

    violations = []

    # Inspect all child nodes inside generate_deye_tou_schedule
    for child in ast.walk(tou_func_node):
        # Look for tuples like (morning_m, ...), (refill_m, ...), (360, ...), (600, ...), (1020, ...), (720, ...)
        if isinstance(child, ast.Tuple) and len(child.elts) >= 4:
            first_elem = child.elts[0]
            if isinstance(first_elem, ast.Constant) and first_elem.value in FORBIDDEN_FIXED_MINUTES:
                violations.append(
                    f"{rel_path}:{child.lineno} hardcodes fixed event minute literal: {first_elem.value}"
                )
            elif isinstance(first_elem, ast.Name) and first_elem.id in FORBIDDEN_EVENT_VARIABLE_NAMES:
                violations.append(
                    f"{rel_path}:{child.lineno} uses fixed clock heuristic variable as event start: '{first_elem.id}'"
                )
        # Look for BinOp multiplying forbidden hours by 60: e.g. 6 * 60, 10 * 60, 17 * 60
        elif isinstance(child, ast.BinOp) and isinstance(child.op, ast.Mult):
            left_val = getattr(child.left, "value", None)
            right_val = getattr(child.right, "value", None)
            if (left_val in FORBIDDEN_FIXED_HOURS and right_val == 60) or (right_val in FORBIDDEN_FIXED_HOURS and left_val == 60):
                violations.append(
                    f"{rel_path}:{child.lineno} hardcodes fixed hour multiplication: {left_val} * {right_val}"
                )
        # Look for calls to _fixed(evening_peak) or similar fixed event wrappers
        elif isinstance(child, ast.Call) and getattr(child.func, "id", None) == "_fixed":
            for arg in child.args:
                if isinstance(arg, ast.Name) and arg.id in FORBIDDEN_EVENT_VARIABLE_NAMES:
                    violations.append(
                        f"{rel_path}:{child.lineno} passes fixed heuristic event '{arg.id}' to _fixed()"
                    )

    assert not violations, (
        "Hardcoded fixed event start hours found in generate_deye_tou_schedule:\n"
        + "\n".join(violations)
    )


def test_no_pv_refill_assumptions_in_tou_logic():
    """
    INVARIANT: No PV refill assumptions in TOU schedule logic.
    Ensures TOU schedule labels and events do not assume solar PV refill
    (e.g. 'Autokonsumpcja PV' or 'refill from PV'). Battery dispatch must
    rely strictly on price windows and verified storage constraints.
    """
    pstryk_path = os.path.join(CONFIG_DIR, "pstryk_engine.py")
    assert os.path.exists(pstryk_path), f"Missing {pstryk_path}"

    with open(pstryk_path, "r", encoding="utf-8") as f:
        src = f.read()

    tree = ast.parse(src, filename=pstryk_path)
    rel_path = os.path.relpath(pstryk_path, REPO_ROOT)

    tou_func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "generate_deye_tou_schedule":
            tou_func_node = node
            break

    assert tou_func_node is not None, "generate_deye_tou_schedule function not found in pstryk_engine.py"

    violations = []

    for child in ast.walk(tou_func_node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            val_str = child.value
            # Flag any assumption of PV generation/refill in TOU labels or text
            if "PV" in val_str or "pv" in val_str.lower():
                if any(w in val_str.lower() for w in ("refill", "autokonsumpcja pv", "ładowanie pv")):
                    violations.append(
                        f"{rel_path}:{child.lineno} contains unverified PV refill assumption: '{val_str}'"
                    )

    assert not violations, (
        "PV refill assumptions found in TOU schedule logic:\n"
        + "\n".join(violations)
    )


TOU_FUNCTIONS = (
    "find_powerbank_best_window",
    "analyse_day_price_profile",
    "generate_deye_tou_schedule",
    "check_and_apply_charge_current_modulation",
    "find_best_discharge_windows",
    "plan_rolling_tou",
    "load_forecast",
    "build_load_profile",
    "plan_charge_hours",  # greedy cheapest-hour charge selection
    "find_charge_window",  # price window per published day (seed/extension from env)
    "charge_current_plan",  # price-weighted Reg 108 currents
    "_priced_hours",
    "_soc_walk",
    "_tou_minute_rows",
    "_tou_row_at",
    "_cached_active_slots",
    "plan_tou",
    "tou_base_power",
    "sync_manual_tou_schedule",
)
AI_TOU_FUNCTIONS = (
    "build_context", "build_prompt", "validate_ai_slots", "simulate_table", "slots_from_ai", "evaluate_ai_slots",
    "choose_plan", "_planned_soc",
)
# Unit-conversion / structural ints: 0, 1, -1 (indices), 60 (min/h), 1440 (min/day), 24 (h/day), 2 (only in comparisons,
# e.g. len(x) == 2). 100 and 1000 are accepted ONLY as the right operand of a division (percent -> fraction, W -> kW).
ALLOWED_INTS = {0, 1, -1, 24, 60, 1440}
ALLOWED_COMPARE_INTS = ALLOWED_INTS | {2}
ALLOWED_DIVISORS = {100, 1000}
ALLOWED_FLOAT_ADDENDS = {0.0, 1.0}
HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")
WEEKDAY_NAMES = {"weekday", "isoweekday", "tm_wday", "wday"}


def _is_int(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)


def _tou_function_violations(fn):
    """AST violations inside one TOU function (see ALLOWED_* above)."""
    found = []
    for node in ast.walk(fn):
        line = getattr(node, "lineno", fn.lineno)
        if isinstance(node, ast.Compare):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and HHMM_RE.match(sub.value):
                    found.append(f"{fn.name}:{line} 'HH:MM' constant {sub.value!r} in comparison")
            for operand in [node.left, *node.comparators]:
                for sub in ast.walk(operand):
                    if _is_int(sub) and sub.value not in ALLOWED_COMPARE_INTS:
                        found.append(f"{fn.name}:{line} integer literal {sub.value} in comparison")
        elif isinstance(node, ast.BinOp):
            for side, operand in (("left", node.left), ("right", node.right)):
                if _is_int(operand) and operand.value not in ALLOWED_INTS:
                    divisor_ok = (
                        side == "right" and isinstance(node.op, (ast.Div, ast.FloorDiv)) and operand.value in ALLOWED_DIVISORS
                    )
                    if not divisor_ok:
                        found.append(f"{fn.name}:{line} integer literal {operand.value} in arithmetic")
                if (
                    isinstance(node.op, (ast.Add, ast.Sub)) and isinstance(operand, ast.Constant)
                    and isinstance(operand.value, float) and operand.value not in ALLOWED_FLOAT_ADDENDS
                ):
                    found.append(f"{fn.name}:{line} float literal {operand.value} added to a price")
        if isinstance(node, ast.Attribute) and node.attr in WEEKDAY_NAMES:
            found.append(f"{fn.name}:{line} weekday dependence '.{node.attr}'")
        elif isinstance(node, ast.Name) and node.id in WEEKDAY_NAMES:
            found.append(f"{fn.name}:{line} weekday dependence '{node.id}'")
    return found


def test_tou_functions_have_no_clock_duration_weekday_or_tolerance_literals():
    """
    INVARIANT (Rule 12): every TOU/dispatch function is data-derived.
    Fails on 'HH:MM' constants in comparisons, integer literals (other than unit conversions) in comparisons or
    arithmetic (e.g. 480, 1140, 180, 240, `// 2`, a literal 100 A), weekday()/tm_wday/wday and float tolerances
    added to prices (e.g. `+ 0.05`). Functions that do not exist yet are skipped.
    """
    pstryk_path = os.path.join(CONFIG_DIR, "pstryk_engine.py")
    with open(pstryk_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=pstryk_path)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in TOU_FUNCTIONS}
    violations = [v for name in TOU_FUNCTIONS if name in functions for v in _tou_function_violations(functions[name])]
    assert not violations, "Hardcoded heuristics in TOU functions:\n" + "\n".join(violations)


def test_ai_tou_planner_functions_have_no_clock_duration_weekday_or_tolerance_literals():
    """INVARIANT (Rule 12): the AI planner's validation, simulation and selection are data-derived too."""
    path = os.path.join(CONFIG_DIR, "ai_tou_planner.py")
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in AI_TOU_FUNCTIONS}
    assert set(functions) == set(AI_TOU_FUNCTIONS), f"missing: {set(AI_TOU_FUNCTIONS) - set(functions)}"
    violations = [v for name in AI_TOU_FUNCTIONS for v in _tou_function_violations(functions[name])]
    assert not violations, "Hardcoded heuristics in ai_tou_planner:\n" + "\n".join(violations)


def test_automatic_charge_path_does_not_use_window_min_hours_or_valley_window():
    """plan_charge_hours / plan_rolling_tou must not reference CHARGE_WINDOW_MIN_HOURS, charge_window_min_hours or
    find_powerbank_best_window (greedy cheapest hours, not a duration/valley window)."""
    pstryk_path = os.path.join(CONFIG_DIR, "pstryk_engine.py")
    with open(pstryk_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=pstryk_path)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "plan_charge_hours" in fns, "plan_charge_hours not implemented"
    banned = {"CHARGE_WINDOW_MIN_HOURS", "charge_window_min_hours", "find_powerbank_best_window"}
    for name in ("plan_rolling_tou", "plan_charge_hours"):
        refs = [getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(fns[name])
                if isinstance(n, (ast.Name, ast.Attribute))]
        assert not banned & set(refs), f"{name} references {banned & set(refs)}"


def test_invariant_scanner_detects_planted_heuristics():
    """Self-check: the scanner flags each forbidden pattern and accepts unit conversions."""
    bad = ast.parse(
        "def f(x, d, p, now_m):\n"
        "    if x == '06:00' or now_m >= 1140: pass\n"
        "    a = p_start // 2\n"
        "    b = d.weekday() + 180\n"
        "    c = p + 0.05\n"
        "    e = 100 * 2 / t\n"
    ).body[0]
    assert len(_tou_function_violations(bad)) >= 6
    good = ast.parse("def f(a, b):\n    m = a * 60 + 1\n    return b / 100.0 + 1.0 if len(a) == 2 and m < 1440 else (b / 1000)\n").body[0]
    assert _tou_function_violations(good) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
