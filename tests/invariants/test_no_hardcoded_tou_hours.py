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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
