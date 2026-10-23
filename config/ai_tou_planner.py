#!/usr/bin/env python3
"""
AI-first Deye TOU planner (OpenAI-compatible chat API, default MiniMax).

The deterministic plan (pstryk_engine.plan_rolling_tou) is always computed first: it is the fallback and the
reference. An AI table is used only when it passes strict bounds validation and its simulated grid cost is not worse
than the algorithm's by more than AI_TOU_MAX_COST_RATIO. The network call is made only on the sync path / explicit
CLI; the sensor path reads the cache file only. The API key is read at call time and never logged or returned.
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import energy_defaults
import pstryk_engine as pe

HHMM_RE = re.compile(r"^\d{2}:\d{2}$")
THINK_RE = re.compile(r"<think>.*?</think>", re.S)
FENCE_RE = re.compile(r"```(?:json)?")


def ai_api_key() -> str:
    return os.environ.get("AI_TOU_API_KEY") or energy_defaults.get_default_str("AI_TOU_API_KEY")


def mask_key(key: str) -> str:
    return "***" if key else ""


def cache_file_path() -> str:
    return os.environ.get("AI_TOU_CACHE_FILE") or energy_defaults.AI_TOU_CACHE_FILE


def _priced_hours(frames: List[Dict[str, Any]]) -> List[Tuple[datetime, datetime, float]]:
    out = {}
    for f in frames or []:
        if not pe._pricing_frame_ok(f):
            continue
        a = pe._iso_dt(f.get("start"))
        if a is None or a.tzinfo is None:
            continue
        out[a] = (a, pe._iso_dt(f.get("end")) or a + timedelta(hours=1), pe.retail_gross_price(f))
    return [out[k] for k in sorted(out)]


def price_fingerprint(frames: List[Dict[str, Any]]) -> str:
    rows = [[pe._iso_z(a), round(p, 6)] for a, _, p in _priced_hours(frames)]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def compact_slots(slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"time": s.get("time"), "target_soc": s.get("target_soc"), "grid_charge": s.get("grid_charge"),
             "charge_current_a": s.get("charge_current_a")} for s in slots or []]


def build_context(frames, now_dt, current_soc, profile, algo, *, active_slots=None, min_soc, max_soc, capacity_kwh,
                  voltage, ceiling_a, loss_pct, **_ignored) -> Dict[str, Any]:
    hours = [h for h in _priced_hours(frames) if h[1] > now_dt]
    loads: Dict[datetime, float] = {}
    if hours:
        for x in pe.load_forecast(now_dt, hours[-1][1], profile):
            key = next((a for a, b, _ in hours if a <= x["start"] < b), None)
            if key is not None:
                loads[key] = loads.get(key, 0.0) + x["kwh"]
    w = pe.WARSAW_TZ
    return {
        "now": now_dt.astimezone(w).isoformat(),
        "hours": [{"start": a.astimezone(w).isoformat(), "end": b.astimezone(w).isoformat(), "price": round(p, 4),
                   "load_kwh": round(loads.get(a, 0.0), 3)} for a, b, p in hours],
        "battery": {"current_soc": current_soc, "min_soc": min_soc, "max_soc": max_soc, "capacity_kwh": capacity_kwh,
                    "voltage": voltage, "charge_current_ceiling_a": ceiling_a, "loss_pct": loss_pct},
        "table": {"slot_count": int(energy_defaults.TOU_SLOT_COUNT),
                  "minute_step": int(energy_defaults.TOU_SLOT_MINUTE_STEP),
                  "soc_step": int(energy_defaults.TOU_FLOOR_SOC_STEP)},
        "active_slots": compact_slots(active_slots),
        "algorithm_reference": {"table": compact_slots((algo or {}).get("deye_tou_schedule")),
                                "plan_timeline": (algo or {}).get("plan_timeline", [])},
    }


def build_prompt(ctx: Dict[str, Any]) -> List[Dict[str, str]]:
    b, t = ctx["battery"], ctx["table"]
    rules = (
        "You plan a Deye hybrid inverter Time-of-Use table for a home battery. Goal: minimise grid cost and protect "
        "battery health. Facts: zero grid export, no PV (the battery is charged only from the grid). "
        f"The table has exactly {t['slot_count']} slots keyed by clock time (HH:MM, Europe/Warsaw, multiples of "
        f"{t['minute_step']} minutes, strictly increasing; the last slot runs through midnight to the first) and it "
        "repeats daily, so it covers the next 24 h. Each slot: target_soc (integer, "
        f"{b['min_soc']}..{b['max_soc']}; in a non-charge slot the battery discharges for the load down to target_soc "
        "then holds), grid_charge (boolean; true charges from the grid up to target_soc), charge_current_a "
        f"(0..{b['charge_current_ceiling_a']}). Charge in the cheapest hours first, sized to the energy need "
        f"(round-trip loss {b['loss_pct']} %), discharge in the dearest hours; peaks are identified per price day. "
        "Battery health: few and short charge blocks, avoid sitting at max SOC, skip cycles that do not pay back "
        f"the loss. Prefer SOC targets on {t['soc_step']} % steps. algorithm_reference is a baseline to beat. "
        'Answer with JSON only: {"slots": [{"time": "HH:MM", "target_soc": int, "grid_charge": bool, '
        '"charge_current_a": int}, ...]}.'
    )
    return [{"role": "system", "content": rules},
            {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)}]


def parse_ai_content(text: Any) -> Optional[Dict[str, Any]]:
    """JSON object of a model answer after stripping <think> blocks and code fences; None when unparseable."""
    if isinstance(text, dict):
        return text
    s = FENCE_RE.sub("", THINK_RE.sub("", str(text or "")))
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        out = json.loads(s[a:b + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _chat(messages: List[Dict[str, str]], key: str) -> Tuple[Optional[str], Optional[str]]:
    """POST /chat/completions -> (content, error). No response_format / temperature / thinking flags (MiniMax)."""
    body = {"model": energy_defaults.AI_TOU_MODEL, "messages": messages,
            "max_completion_tokens": int(energy_defaults.AI_TOU_MAX_TOKENS)}
    if energy_defaults.AI_TOU_REASONING_EFFORT:
        body["reasoning_effort"] = energy_defaults.AI_TOU_REASONING_EFFORT
    base = energy_defaults.AI_TOU_BASE_URL.rstrip("/")
    if not base.lower().startswith("https://"):
        return None, "ai_insecure_base_url"  # the bearer key must never travel in clear text
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    timeout = float(energy_defaults.AI_TOU_TIMEOUT_S)
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                box["data"] = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # keep status + provider message for diagnosis, key scrubbed
            detail = e.read().decode("utf-8", "replace")[:300].replace(key, "***")
            box["error"] = f"ai_call_failed: HTTP {e.code} {detail}".strip()
        except Exception as e:  # timeout, parse: never echo the key (exceptions do not carry headers)
            box["error"] = f"ai_call_failed: {type(e).__name__}"

    # urlopen's timeout is per socket op; a daemon thread + join bounds the whole call (shell_command hard kill)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None, "ai_call_failed: deadline"
    if "error" in box:
        return None, box["error"]
    try:
        choice = box["data"]["choices"][0]
        content = (choice.get("message") or {}).get("content")
    except Exception as e:
        return None, f"ai_call_failed: {type(e).__name__}"
    if choice.get("finish_reason") == "length":
        return None, "ai_truncated"
    if not content or not str(content).strip():
        return None, "ai_empty_answer"
    return str(content), None


def call_ai(messages: List[Dict[str, str]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    key = ai_api_key()
    if not key:
        return None, "ai_disabled"
    content, err = _chat(messages, key)
    if err:
        print(f"[ai-tou] {err}", file=sys.stderr)
        return None, err
    parsed = parse_ai_content(content)
    return (parsed, None) if parsed is not None else (None, "ai_bad_json")


def validate_ai_slots(slots: Any, ctx: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Bounds-only validation of a 6-slot answer (AI or manual)."""
    b, t = ctx["battery"], ctx["table"]
    if not isinstance(slots, list) or len(slots) != t["slot_count"]:
        return False, "slot_count"
    prev = None
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            return False, f"slot_{i + 1}_type"
        tm = str(s.get("time", ""))
        m = pe.hhmm_to_minutes(tm) if HHMM_RE.match(tm) else None
        if m is None or not 0 <= m < 1440 or m % max(1, t["minute_step"]):
            return False, f"slot_{i + 1}_time"
        if prev is not None and m <= prev:
            return False, "slot_order"
        prev = m
        if not isinstance(s.get("grid_charge"), bool):
            return False, f"slot_{i + 1}_grid_charge"
        soc = s.get("target_soc")
        if isinstance(soc, bool) or not isinstance(soc, (int, float)) or not b["min_soc"] <= soc <= b["max_soc"]:
            return False, f"slot_{i + 1}_soc_bounds"
        cur = s.get("charge_current_a")
        if cur is not None and (isinstance(cur, bool) or not isinstance(cur, (int, float))
                                or not 0 <= cur <= b["charge_current_ceiling_a"]):
            return False, f"slot_{i + 1}_charge_current"
    try:
        pe._tou_minute_rows(slots)
    except (ValueError, TypeError, AttributeError):
        return False, "slot_time_parse"
    return True, None


def simulate_table(slots, frames, now_dt, current_soc, profile, *, min_soc, max_soc, capacity_kwh, voltage, ceiling_a,
                   loss_pct, **_ignored) -> Tuple[float, List[List[Any]]]:
    """Grid cost of running a clock table over [now, end of prices) with Deye floor semantics (same SOC walk as the
    engine). Returns (cost, [[hour start ISO, SOC]])."""
    hours = [h for h in _priced_hours(frames) if h[1] > now_dt]
    if not hours:
        return 0.0, []
    rows = pe._tou_minute_rows(slots)
    hour_key = lambda t: t.astimezone(pe.timezone.utc).replace(minute=0, second=0, microsecond=0)
    rates = {}
    fc = pe.load_forecast(now_dt, hours[-1][1], profile)
    for i, x in enumerate(fc):
        end = fc[i + 1]["start"] if i + 1 < len(fc) else hours[-1][1]
        rates[hour_key(x["start"])] = x["kwh"] / max((end - x["start"]).total_seconds() / 3600.0, 1e-9)
    rate_at = lambda t: rates.get(hour_key(t), 0.0)
    step = timedelta(minutes=max(1, int(energy_defaults.TOU_SLOT_MINUTE_STEP)))
    cells, price_of = [], []
    for a, b, price in hours:
        t = max(a, now_dt)
        while t < b:
            e = min(b, t + step)
            loc = t.astimezone(pe.WARSAW_TZ)
            r = pe._tou_row_at(rows, loc.hour * 60 + loc.minute)
            cells.append({"start": t, "end": e, "target_soc": r[2], "grid_charge": r[1], "load_kw": rate_at(t)})
            price_of.append(price)
            t = e
    loss = float(loss_pct) / 100.0
    charge_kw = min(float(ceiling_a), float(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A)) * float(voltage) / 1000.0 / (1.0 + loss)
    proj = pe.project_soc_timeline(cells, current_soc, 0.0, now_dt, min_soc=min_soc, max_soc=max_soc,
                                   capacity_kwh=capacity_kwh, charge_kw=charge_kw)
    cap, cost, points = float(capacity_kwh), 0.0, []
    for c, p, price in zip(cells, proj, price_of):
        load = c["load_kw"] * (c["end"] - c["start"]).total_seconds() / 3600.0
        d = (p["soc_end"] - p["soc_start"]) * cap / 100.0
        grid = load + d * (1.0 + loss) if d > 0 else max(0.0, load + d)
        cost += grid * price
        if not points or c["start"].minute == 0:
            points.append([pe._iso_z(c["start"]), round(p["soc_start"], 1)])
    return cost, points


def slots_from_ai(ai_slots, *, min_soc, voltage, discharge_a, base_power_w, calibration_active=False):
    """Validated compact slots -> full engine slot dicts via _tou_table_slots (grid charge at the ceiling)."""
    ceiling = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))
    segs = []
    for s in ai_slots:
        grid = bool(s["grid_charge"])
        tgt = int(s["target_soc"])
        role = "charge" if grid else ("hold" if tgt > int(min_soc) else "peak")
        segs.append({"clock_m": pe.hhmm_to_minutes(s["time"]), "grid_charge": grid, "target_soc": tgt,
                     "role": role, "charge_a": ceiling if grid else 0})
    return pe._tou_table_slots(segs, min_soc=min_soc, voltage=voltage, discharge_a=discharge_a,
                               base_power_w=base_power_w, calibration_active=calibration_active)


def evaluate_ai_slots(ai_slots, algo, ctx, frames, now_dt, current_soc, profile, kw) -> Dict[str, Any]:
    """{ok, reason, slots, ai_cost, algorithm_cost, soc_points}: bounds + economic guard.
    ponytail: no separate Min SOC coverage check - targets are validated >= Min SOC so the Deye floor walk cannot
    break it; the cost ratio guard covers worse coverage. Add one if the guard proves too loose."""
    ok, reason = validate_ai_slots(ai_slots, ctx)
    if not ok:
        return {"ok": False, "reason": f"invalid_{reason}"}
    ai_cost, points = simulate_table(ai_slots, frames, now_dt, current_soc, profile, **kw)
    algo_cost, _ = simulate_table(algo["deye_tou_schedule"], frames, now_dt, current_soc, profile, **kw)
    res = {"ai_cost": round(ai_cost, 4), "algorithm_cost": round(algo_cost, 4), "soc_points": points}
    margin = abs(algo_cost) * (float(energy_defaults.AI_TOU_MAX_COST_RATIO) - 1.0)
    if ai_cost - algo_cost > margin + pe._KWH_EPS:
        return dict(res, ok=False, reason=f"cost_ratio ai={ai_cost:.2f} algorithm={algo_cost:.2f}")
    ref = algo["deye_tou_schedule"][0]
    slots = slots_from_ai(ai_slots, min_soc=kw["min_soc"], voltage=ref["voltage"],
                          discharge_a=ref["discharge_current_a"], base_power_w=ref["power_w"],
                          calibration_active=bool(kw.get("calibration_active")))
    return dict(res, ok=True, reason=None, slots=slots)


def read_ai_cache() -> Dict[str, Any]:
    try:
        with open(cache_file_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_ai_cache(record: Dict[str, Any]) -> None:
    """Write-on-change (flash wear)."""
    if read_ai_cache() == json.loads(json.dumps(record)):
        return
    try:
        with open(cache_file_path(), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
    except OSError as e:
        print(f"[ai-tou] cache write failed: {e}", file=sys.stderr)


def _planned_soc(cache: Dict[str, Any], now_dt: datetime) -> Optional[float]:
    pts = [p for p in cache.get("soc_points") or [] if pe._iso_dt(p[0]) and pe._iso_dt(p[0]) <= now_dt]
    return float(pts[-1][1]) if pts else None


def choose_plan(algo, frames, now_dt, current_soc, profile, *, allow_ai_call=False, force_ai=False, ai_response=None,
                update_cache=True, active_slots=None, sync_started=None, **kw) -> Dict[str, Any]:
    """AI-first selection over the algorithm plan (see module doc). Network only when allow_ai_call.
    sync_started (time.monotonic() at sync start): the network call is skipped when the AI timeout plus the local
    Modbus budget would no longer fit in the shell_command budget."""
    out = dict(algo, plan_source="algorithm", fallback_reason=None, ai_last_call=None,
               algorithm_schedule=algo["deye_tou_schedule"])

    def fallback(reason, last=None):
        out.update(fallback_reason=reason, ai_last_call=last)
        return out

    if kw.get("calibration_active"):
        return fallback("calibration")
    if ai_response is None and not ai_api_key():
        return fallback("ai_disabled")
    fp = price_fingerprint(frames)
    cache = {} if ai_response is not None else read_ai_cache()
    same = cache.get("fingerprint") == fp
    last = pe._iso_dt(cache["last_call"]) if cache.get("last_call") else None
    elapsed = last is None or now_dt - last >= timedelta(minutes=float(energy_defaults.AI_TOU_MIN_INTERVAL_MIN))
    planned = _planned_soc(cache, now_dt) if same else None
    deviated = planned is not None and float(current_soc) < planned - float(energy_defaults.AI_TOU_DEVIATION_SOC)
    status = cache.get("status")
    call = allow_ai_call and (force_ai or ai_response is not None or not same
                              or (elapsed and (status != "ok" or deviated)))
    ctx = build_context(frames, now_dt, current_soc, profile, algo, active_slots=active_slots, **kw)
    if call:
        if ai_response is not None:
            raw, err = parse_ai_content(ai_response), None
            err = None if raw is not None else "ai_bad_json"
        elif sync_started is not None and (time.monotonic() - sync_started + float(energy_defaults.AI_TOU_TIMEOUT_S)
                                           + float(energy_defaults.LOCAL_TIME_BUDGET_S)
                                           > float(energy_defaults.SHELL_COMMAND_TIMEOUT_S)):
            return fallback("ai_skipped_budget", cache.get("last_call"))
        else:
            raw, err = call_ai(build_prompt(ctx))
        last_iso = now_dt.isoformat()
        ai_slots = raw.get("slots") if isinstance(raw, dict) else None
        if err is None and ai_slots is None:
            err = "ai_bad_response"
        ev = {"ok": False, "reason": err} if err else evaluate_ai_slots(ai_slots, algo, ctx, frames, now_dt,
                                                                        current_soc, profile, kw)
        if update_cache:
            rec = {"fingerprint": fp, "last_call": last_iso, "status": "ok" if ev["ok"] else "failed",
                   "reason": ev["reason"]}
            if ev["ok"]:
                rec.update(slots=ai_slots, soc_points=ev["soc_points"], ai_cost=ev["ai_cost"],
                           algorithm_cost=ev["algorithm_cost"])
            write_ai_cache(rec)
    else:
        if not same:
            return fallback("ai_no_plan_for_prices")
        last_iso = cache.get("last_call")
        if status != "ok":
            return fallback(cache.get("reason") or "ai_failed", last_iso)
        ev = evaluate_ai_slots(cache.get("slots"), algo, ctx, frames, now_dt, current_soc, profile, kw)
    if not ev["ok"]:
        return fallback(ev["reason"], last_iso)
    out.update(deye_tou_schedule=ev["slots"], plan_source="ai", ai_last_call=last_iso, ai_cost=ev["ai_cost"],
               algorithm_cost=ev["algorithm_cost"])
    return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="AI TOU planner utilities")
    ap.add_argument("--test-call", action="store_true", help="Minimal connectivity/key check (prints status, never the key)")
    args = ap.parse_args(argv)
    if not args.test_call:
        ap.print_help()
        return 0
    key = ai_api_key()
    masked = mask_key(key)
    t0 = time.monotonic()
    content, err = _chat([{"role": "user", "content": 'Reply with JSON {"ok": true}'}], key) if key else (None, "ai_disabled")
    print(json.dumps({"status": "ok" if err is None else err, "latency_ms": int((time.monotonic() - t0) * 1000),
                      "model": energy_defaults.AI_TOU_MODEL, "base_url": energy_defaults.AI_TOU_BASE_URL,
                      "api_key": masked, "answer": (content or "")[:200]}, ensure_ascii=False))
    return 0 if err is None else 1


if __name__ == "__main__":
    sys.exit(main())
