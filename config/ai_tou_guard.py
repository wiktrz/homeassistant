#!/usr/bin/env python3
"""
Guard for AI-proposed Deye TOU tables (stdlib only, read-only: never touches the inverter or any register).

validate(slots, ctx) -> list of error codes (empty = accepted). ctx is the dict ai_tou_planner.build_context() sends
to the model: battery.min_soc/max_soc, table.slot_count/minute_step/soc_step, now, active_slots,
charge_windows [{start, end}] (offset-aware ISO, Europe/Warsaw), algorithm_reference.table.
Rules: exactly slot_count slots with keys {time, target_soc, grid_charge, charge_current_a}; HH:MM on the minute
step grid, strictly increasing; target_soc an int in [Min, Max]; grid_charge a bool; every grid-charge slot targets
Max; grid charge covers every computed charge window and nothing else except the algorithm's own pre-window top-up;
the slot active now keeps the active table's behaviour. charge_current_a is ignored (the engine owns Reg 108).
The manual editor does not use this guard (it stays bounds-only in ai_tou_planner.validate_ai_slots).
CLI: ai_tou_guard.py --response <json> --context <json>; exit 0 ok, 1 errors printed.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

ALLOWED_KEYS = {"time", "target_soc", "grid_charge", "charge_current_a"}
HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")
DAY_MIN = 1440


def _minutes(tm: Any) -> Optional[int]:
    if not isinstance(tm, str) or not HHMM_RE.match(tm):
        return None
    hh, mm = tm.split(":")
    m = int(hh) * 60 + int(mm)
    return m if 0 <= m < DAY_MIN and int(mm) < 60 else None


def grid_minutes(slots: List[Dict[str, Any]]) -> Set[int]:
    """Clock minutes of the daily circle with grid charge on (slot runs to the next slot, the last wraps)."""
    rows = sorted((_minutes(s.get("time")), bool(s.get("grid_charge"))) for s in slots or [])
    out: Set[int] = set()
    for i, (m, grid) in enumerate(rows):
        if grid and m is not None:
            end_m = rows[(i + 1) % len(rows)][0]
            if end_m <= m:
                end_m += DAY_MIN
            out.update(x % DAY_MIN for x in range(m, end_m))
    return out


def window_minutes(windows: List[Dict[str, Any]], now: Optional[datetime] = None) -> Set[int]:
    """Clock minutes of the windows, clipped to the 24 h the clock table covers from now."""
    out: Set[int] = set()
    for w in windows or []:
        a, b = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        if now is not None:
            a, b = max(a, now), min(b, now + timedelta(days=1))
        start_m = a.hour * 60 + a.minute
        out.update((start_m + k) % DAY_MIN for k in range(int((b - a).total_seconds() // 60)))
    return out


def _active_row(slots: List[Dict[str, Any]], minute: int):
    rows = sorted((_minutes(s.get("time")), bool(s.get("grid_charge")), s.get("target_soc")) for s in slots)
    return next((r for r in reversed(rows) if r[0] <= minute), rows[-1])


def validate(slots: Any, ctx: Dict[str, Any]) -> List[str]:
    b, t = ctx["battery"], ctx["table"]
    if not isinstance(slots, list) or len(slots) != t["slot_count"]:
        return ["slot_count"]
    errors: List[str] = []
    structural = False  # times / grid flags unusable for the window and active-slot checks
    prev = None
    for i, s in enumerate(slots, 1):
        if not isinstance(s, dict):
            return [f"slot_{i}_type"]
        if not set(s) <= ALLOWED_KEYS:
            errors.append(f"slot_{i}_keys")
        m = _minutes(s.get("time"))
        if m is None or m % max(1, t["minute_step"]):
            errors.append(f"slot_{i}_time")
            structural = True
        elif prev is not None and m <= prev:
            errors.append("slot_order")
            structural = True
        prev = m if m is not None else prev
        soc = s.get("target_soc")
        if type(soc) is not int:
            errors.append(f"slot_{i}_soc_type")
            structural = True
        elif not b["min_soc"] <= soc <= b["max_soc"]:
            errors.append(f"slot_{i}_soc_bounds")
        if not isinstance(s.get("grid_charge"), bool):
            errors.append(f"slot_{i}_grid_charge")
            structural = True
        elif s["grid_charge"] and soc != b["max_soc"]:
            errors.append(f"slot_{i}_charge_target_not_max")
    if structural:
        return errors
    now = datetime.fromisoformat(ctx["now"]) if ctx.get("now") else None
    windows = window_minutes(ctx.get("charge_windows"), now)
    if windows:
        grid = grid_minutes(slots)
        algo_grid = grid_minutes((ctx.get("algorithm_reference") or {}).get("table"))
        # A clock table cannot hold two days of one clock minute: the algorithm table decides which window minutes
        # exist in it (required); the AI must cover those and may charge only in windows or the algorithm's top-up.
        if (windows & algo_grid) - grid:
            errors.append("charge_window_not_covered")
        if grid - (windows | algo_grid):
            errors.append("grid_charge_outside_window")
    active = ctx.get("active_slots") or []
    if len(active) == t["slot_count"] and now:
        minute = now.hour * 60 + now.minute
        try:
            new, old = _active_row(slots, minute), _active_row(active, minute)
        except (TypeError, AttributeError):
            new = old = None  # unusable inverter slots (no time): nothing to compare against
        if new and old and type(new[2]) is int and type(old[2]) is int:
            tol = 0 if new[1] or old[1] else t.get("soc_step", 0)
            if new[1] != old[1] or abs(new[2] - old[2]) > tol:
                errors.append("active_slot_changed")
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--response", required=True)
    ap.add_argument("--context", required=True)
    a = ap.parse_args(argv)
    with open(a.response, encoding="utf-8") as f:
        resp = json.load(f)
    with open(a.context, encoding="utf-8") as f:
        ctx = json.load(f)
    errors = validate(resp.get("slots") if isinstance(resp, dict) else resp, ctx)
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
