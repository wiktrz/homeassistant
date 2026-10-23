"""
AI-first TOU planner (plan /.claude/plans/try-to-continue-this-zippy-cloud.md sections 1, 2, 5, 5b).
Hermetic: urlopen is mocked, the cache file lives in tmp_path, the AI key is a fake test value.
Fixture: live_2026_10_23.json (real Pstryk frames) + deye/real_profile.json, now = Fri 23.10 12:18, SOC 17.
"""
import io
import json
import os
import socket
import sys
from datetime import datetime, timedelta
from unittest import mock

import pytest

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config"))
FIX = os.path.abspath(os.path.join(os.path.dirname(__file__), "../fixtures"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
import ai_tou_planner  # noqa: E402
import energy_defaults  # noqa: E402
import pstryk_engine  # noqa: E402

W = pstryk_engine.WARSAW_TZ
FAKE_KEY = "sk-test-not-a-real-key-123456"
P = dict(min_soc=10, max_soc=90, capacity_kwh=16.13, voltage=51.2,
         ceiling_a=energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A, loss_pct=10.0)
CEIL = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A))


def _load(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as f:
        return json.load(f)


PROFILE = _load("deye", "real_profile.json")
FX = _load("pstryk", "live_2026_10_23.json")
FRAMES = FX["pricing_frames_today"] + FX["pricing_frames_tomorrow"]
NOW, SOC = datetime(2026, 10, 23, 12, 18, tzinfo=W), 17
PRICES_FILE = os.path.join(FIX, "pstryk", "live_2026_10_23.json")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_tou_planner, "cache_file_path", lambda: str(tmp_path / "ai_plan.json"))
    monkeypatch.setattr(ai_tou_planner, "ai_api_key", lambda: FAKE_KEY)
    monkeypatch.setattr(pstryk_engine, "LAST_TOU_SYNC_PATHS", [str(tmp_path / "last_sync.json")])
    yield


def _algo():
    return pstryk_engine.plan_rolling_tou(FRAMES, NOW, SOC, PROFILE, **P)


def _ai_slots_from(slots):
    return [{"time": s["time"], "target_soc": s["target_soc"], "grid_charge": s["grid_charge"],
             "charge_current_a": s["charge_current_a"]} for s in slots]


def _response(content):
    body = {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}]}
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.status = 200
    resp.__enter__.return_value = resp
    return resp


class _Urlopen:
    def __init__(self, content=None, exc=None):
        self.content, self.exc, self.calls, self.requests = content, exc, 0, []

    def __call__(self, req, timeout=None):
        self.calls += 1
        self.requests.append(req)
        if self.exc:
            raise self.exc
        return _response(self.content)


def _plan(urlopen, frames=None, soc=SOC, now=NOW, allow=True, force=False, monkeypatch=None):
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", urlopen)
    return pstryk_engine.plan_tou(frames or FRAMES, now, soc, PROFILE, allow_ai_call=allow, force_ai=force, **P)


# ---- prompt ----

def test_prompt_has_prices_load_limits_and_no_secret():
    algo = _algo()
    ctx = ai_tou_planner.build_context(FRAMES, NOW, SOC, PROFILE, algo, active_slots=None, **P)
    text = json.dumps(ai_tou_planner.build_prompt(ctx), ensure_ascii=False)
    assert FAKE_KEY not in text
    data = json.loads(ai_tou_planner.build_prompt(ctx)[-1]["content"])
    assert data["battery"]["min_soc"] == P["min_soc"] and data["battery"]["max_soc"] == P["max_soc"]
    assert data["battery"]["charge_current_ceiling_a"] == P["ceiling_a"]
    assert len(data["hours"]) > 0 and all("price" in h and "load_kwh" in h for h in data["hours"])
    assert any(h["load_kwh"] > 0 for h in data["hours"])
    assert data["table"]["slot_count"] == energy_defaults.TOU_SLOT_COUNT
    assert len(data["algorithm_reference"]["table"]) == energy_defaults.TOU_SLOT_COUNT


# ---- valid answer ----

def test_valid_ai_answer_becomes_plan_source_ai(monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])})
    plan = _plan(up, monkeypatch=monkeypatch)
    assert up.calls == 1
    assert plan["plan_source"] == "ai" and plan["fallback_reason"] is None
    sched = plan["deye_tou_schedule"]
    assert len(sched) == energy_defaults.TOU_SLOT_COUNT
    assert set(sched[0]) == set(algo["deye_tou_schedule"][0])  # same builder shape (_tou_table_slots)
    assert all(s["charge_current_a"] == CEIL for s in sched if s["grid_charge"])
    assert FAKE_KEY in up.requests[0].get_header("Authorization")
    assert FAKE_KEY not in json.dumps(plan, default=str)


# ---- invalid answers fall back ----

def _mutate(kind, slots):
    s = [dict(x) for x in slots]
    if kind == "count":
        return s[:-1]
    if kind == "order":
        s[0]["time"], s[1]["time"] = s[1]["time"], s[0]["time"]
    elif kind == "soc_bounds":
        s[0]["target_soc"] = P["max_soc"] + energy_defaults.TOU_FLOOR_SOC_STEP
    elif kind == "current":
        s[0]["charge_current_a"] = CEIL + 1
    elif kind == "grid_type":
        s[0]["grid_charge"] = "yes"
    elif kind == "time_grid":
        s[1]["time"] = s[1]["time"][:3] + "%02d" % (int(s[1]["time"][3:]) + 1)
    return s


@pytest.mark.parametrize("kind", ["count", "order", "soc_bounds", "current", "grid_type", "time_grid"])
def test_invalid_ai_answer_falls_back_with_reason(kind, monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _mutate(kind, _ai_slots_from(algo["deye_tou_schedule"]))})
    plan = _plan(up, monkeypatch=monkeypatch)
    assert plan["plan_source"] == "algorithm" and plan["fallback_reason"]
    assert plan["deye_tou_schedule"] == algo["deye_tou_schedule"]


def test_timeout_falls_back(monkeypatch):
    plan = _plan(_Urlopen(exc=socket.timeout("t")), monkeypatch=monkeypatch)
    assert plan["plan_source"] == "algorithm" and "ai_call_failed" in plan["fallback_reason"]


def test_bad_json_falls_back(monkeypatch):
    plan = _plan(_Urlopen("not json at all {"), monkeypatch=monkeypatch)
    assert plan["plan_source"] == "algorithm" and plan["fallback_reason"]


def test_json_in_code_fence_is_accepted(monkeypatch):
    algo = _algo()
    content = "```json\n" + json.dumps({"slots": _ai_slots_from(algo["deye_tou_schedule"])}) + "\n```"
    assert _plan(_Urlopen(content), monkeypatch=monkeypatch)["plan_source"] == "ai"


def test_cost_ratio_breach_falls_back(monkeypatch):
    algo = _algo()
    # charge to max in every slot: grid buys all day at every price -> dearer than the algorithm
    bad = [{"time": s["time"], "target_soc": P["max_soc"], "grid_charge": True, "charge_current_a": CEIL}
           for s in algo["deye_tou_schedule"]]
    plan = _plan(_Urlopen({"slots": bad}), monkeypatch=monkeypatch)
    assert plan["plan_source"] == "algorithm" and "cost" in plan["fallback_reason"]


def test_no_key_means_algorithm_without_call(monkeypatch):
    monkeypatch.setattr(ai_tou_planner, "ai_api_key", lambda: "")
    up = _Urlopen({"slots": []})
    plan = _plan(up, monkeypatch=monkeypatch)
    assert up.calls == 0 and plan["plan_source"] == "algorithm" and plan["fallback_reason"] == "ai_disabled"


# ---- cache ----

def test_cache_reuse_on_same_fingerprint(monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])})
    _plan(up, monkeypatch=monkeypatch)
    plan = _plan(up, now=NOW + timedelta(minutes=5), monkeypatch=monkeypatch)
    assert up.calls == 1 and plan["plan_source"] == "ai"


def test_price_change_recalls(monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])})
    _plan(up, monkeypatch=monkeypatch)
    frames = json.loads(json.dumps(FRAMES))
    frames[-1]["metrics"]["pricing"]["price_gross"] = frames[-1]["metrics"]["pricing"]["price_gross"] * 2
    _plan(up, frames=frames, monkeypatch=monkeypatch)
    assert up.calls == 2


def test_cache_written_only_on_change(monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])})
    _plan(up, monkeypatch=monkeypatch)
    path = ai_tou_planner.cache_file_path()
    mtime = os.stat(path).st_mtime_ns
    _plan(up, now=NOW + timedelta(minutes=5), monkeypatch=monkeypatch)
    assert os.stat(path).st_mtime_ns == mtime


def test_deviation_recalls_after_interval_and_is_rate_limited(monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])})
    _plan(up, soc=60, monkeypatch=monkeypatch)
    low = 60 - energy_defaults.AI_TOU_DEVIATION_SOC - P["max_soc"]  # far below any planned SOC
    low = max(0, low)
    # within the interval: rate limited
    _plan(up, soc=low, now=NOW + timedelta(minutes=1), monkeypatch=monkeypatch)
    assert up.calls == 1
    later = NOW + timedelta(minutes=energy_defaults.AI_TOU_MIN_INTERVAL_MIN + 1)
    _plan(up, soc=low, now=later, monkeypatch=monkeypatch)
    assert up.calls == 2


def test_negative_cache_blocks_retry_within_interval(monkeypatch):
    up = _Urlopen(exc=socket.timeout("t"))
    _plan(up, monkeypatch=monkeypatch)
    plan = _plan(up, now=NOW + timedelta(minutes=1), monkeypatch=monkeypatch)
    assert up.calls == 1 and plan["plan_source"] == "algorithm" and plan["fallback_reason"]
    _plan(up, now=NOW + timedelta(minutes=energy_defaults.AI_TOU_MIN_INTERVAL_MIN + 1), monkeypatch=monkeypatch)
    assert up.calls == 2


def test_force_bypasses_cache(monkeypatch):
    algo = _algo()
    up = _Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])})
    _plan(up, monkeypatch=monkeypatch)
    _plan(up, force=True, monkeypatch=monkeypatch)
    assert up.calls == 2


def test_sensor_path_never_opens_socket(monkeypatch):
    algo = _algo()
    _plan(_Urlopen({"slots": _ai_slots_from(algo["deye_tou_schedule"])}), monkeypatch=monkeypatch)

    def boom(*a, **k):
        raise AssertionError("network used on the sensor path")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(pstryk_engine, "datetime", _FrozenDT)
    monkeypatch.setattr(pstryk_engine, "_cached_active_slots", lambda: None)
    monkeypatch.setattr(pstryk_engine, "load_household_profile", lambda: PROFILE)
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", boom)
    fields = pstryk_engine.rolling_plan_fields(
        {"pricing_frames": FRAMES}, current_soc=SOC, min_soc=P["min_soc"], max_soc=P["max_soc"],
        capacity_kwh=P["capacity_kwh"], voltage=P["voltage"], loss_pct=P["loss_pct"], calibration_active=False,
        max_discharge_current=None)
    assert fields["plan_source"] == "ai" and fields["ai_last_call"]
    assert len(fields["deye_tou_schedule"]) == energy_defaults.TOU_SLOT_COUNT


class _FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


# ---- manual sync ----

def _manual(slots):
    return [{"time": s["time"], "target_soc": s["target_soc"], "grid_charge": s["grid_charge"]} for s in slots]


def test_manual_sync_rejects_bad_json_and_bounds():
    res = pstryk_engine.sync_manual_tou_schedule("[{not json", min_soc=P["min_soc"], max_soc=P["max_soc"], dry_run=True)
    assert res["success"] is False
    bad = _manual(_algo()["deye_tou_schedule"])
    bad[2]["target_soc"] = P["min_soc"] - 1
    res = pstryk_engine.sync_manual_tou_schedule(json.dumps(bad), min_soc=P["min_soc"], max_soc=P["max_soc"], dry_run=True)
    assert res["success"] is False and res["error"]


def test_manual_sync_writes_six_slots_at_ceiling_without_reg108():
    slots = _manual(_algo()["deye_tou_schedule"])
    client = _client()
    with mock.patch("pstryk_engine.solarman_v5_client", client), \
            mock.patch("pstryk_engine.set_charge_current_with_fallback") as reg108:
        res = pstryk_engine.sync_manual_tou_schedule(json.dumps(slots), host="127.0.0.1", port=1,
                                                     min_soc=P["min_soc"], max_soc=P["max_soc"], auto_sync_off=True)
    assert res["success"] is True and res["plan_source"] == "manual"
    written = client.sync_tou_schedule.call_args.kwargs["slots"]
    assert len(written) == energy_defaults.TOU_SLOT_COUNT
    assert [s["time"] for s in written] == [s["time"] for s in slots]
    assert all(s["charge_current_a"] == CEIL for s in written if s["grid_charge"])
    reg108.assert_not_called()
    client.set_charge_current.assert_not_called()


def test_manual_dry_run_does_not_write():
    client = mock.MagicMock()
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        res = pstryk_engine.sync_manual_tou_schedule(json.dumps(_manual(_algo()["deye_tou_schedule"])),
                                                     min_soc=P["min_soc"], max_soc=P["max_soc"], dry_run=True)
    assert res["success"] is True and res["dry_run"] is True and len(res["slots"]) == energy_defaults.TOU_SLOT_COUNT
    client.sync_tou_schedule.assert_not_called()


# ---- CLI (argv) ----

CLI_BASE = ["--prices-file", PRICES_FILE, "--now", NOW.isoformat(), "--current-soc", str(SOC),
            "--min-soc", str(P["min_soc"]), "--max-soc", str(P["max_soc"]),
            "--battery-capacity", str(P["capacity_kwh"]), "--nominal-voltage", str(P["voltage"]),
            "--loss-buffer-pct", str(P["loss_pct"])]


@pytest.fixture
def cli_env(monkeypatch):
    monkeypatch.setattr(pstryk_engine, "load_household_profile", lambda: PROFILE)
    monkeypatch.setattr(pstryk_engine, "solarman_v5_client", None)

    def boom(*a, **k):
        raise AssertionError("no network in CLI tests")
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", boom)
    monkeypatch.setattr(pstryk_engine, "fetch_consolidated_data", boom)


def test_cli_ai_prompt_prints_prompt_without_key(cli_env, capsys):
    assert pstryk_engine.main(["--ai-prompt"] + CLI_BASE) == 0
    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    msgs = json.loads(out)["messages"]
    assert msgs[0]["role"] == "system" and "hours" in json.loads(msgs[-1]["content"])
    assert json.loads(out)["api_key"].startswith("***")


def test_cli_print_tou_ai_with_response_file(cli_env, capsys, tmp_path):
    algo = _algo()
    rf = tmp_path / "answer.json"
    rf.write_text(json.dumps({"slots": _ai_slots_from(algo["deye_tou_schedule"])}))
    assert pstryk_engine.main(["--print-tou", "--ai", "--ai-response-file", str(rf)] + CLI_BASE) == 0
    out = capsys.readouterr().out
    assert "plan_source: ai" in out and "ALGORYTM" in out and "AI" in out
    assert not os.path.exists(ai_tou_planner.cache_file_path())  # read-only: cache not updated


def test_cli_print_tou_ai_bad_response_falls_back(cli_env, capsys, tmp_path):
    rf = tmp_path / "answer.json"
    rf.write_text(json.dumps({"slots": [{"time": "00:00"}]}))
    assert pstryk_engine.main(["--print-tou", "--ai", "--ai-response-file", str(rf)] + CLI_BASE) == 0
    out = capsys.readouterr().out
    assert "plan_source: algorithm" in out and "fallback_reason" in out


def test_cli_sync_manual_dry_run(cli_env, capsys):
    slots = json.dumps(_manual(_algo()["deye_tou_schedule"]))
    assert pstryk_engine.main(["--sync-manual-tou", slots, "--dry-run", "--min-soc", "10", "--max-soc", "90"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["dry_run"] is True and len(res["slots"]) == energy_defaults.TOU_SLOT_COUNT
    assert pstryk_engine.main(["--sync-manual-tou", "[]", "--dry-run", "--min-soc", "10", "--max-soc", "90"]) == 1


def test_cli_sync_force_forces_fresh_ai_call(monkeypatch):
    seen = {}

    def fake_sync(**kw):
        seen.update(kw)
        return {"success": True}
    monkeypatch.setattr(pstryk_engine, "sync_deye_inverter_tou_schedule", fake_sync)
    assert pstryk_engine.main(["--sync-deye-tou", "--force"]) == 0
    assert seen["force"] is True


def test_sync_passes_force_ai_to_plan_tou(tmp_path, monkeypatch):
    seen = {}
    algo = _algo()

    def fake_plan_tou(*a, **kw):
        seen.update(kw)
        return dict(algo, plan_source="algorithm", fallback_reason="x", ai_last_call=None)
    monkeypatch.setattr(pstryk_engine, "plan_tou", fake_plan_tou)
    monkeypatch.setattr(pstryk_engine, "read_cache", lambda *a, **k: {"pricing_frames": FRAMES})
    monkeypatch.setattr(pstryk_engine, "cache_is_reusable", lambda *a, **k: True)
    monkeypatch.setattr(pstryk_engine, "load_household_profile", lambda: PROFILE)
    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {"success": True, "config": {}, "telemetry": {"battery_soc": 50}}
    client.sync_tou_schedule.return_value = {"success": True}
    client.set_charge_current.return_value = {"success": True}
    with mock.patch("pstryk_engine.solarman_v5_client", client), \
            mock.patch("pstryk_engine.set_charge_current_with_fallback", return_value={"success": True}):
        res = pstryk_engine.sync_deye_inverter_tou_schedule(cache_dir=str(tmp_path), force=True)
    assert seen["allow_ai_call"] is True and seen["force_ai"] is True
    assert res["plan_source"] == "algorithm"


def test_test_call_prints_status_not_key(monkeypatch, capsys):
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", _Urlopen('{"ok": true}'))
    assert ai_tou_planner.main(["--test-call"]) == 0
    out = capsys.readouterr().out
    assert FAKE_KEY not in out and "latency_ms" in out and "status" in out


# ---- review fixes ----


def _client(battery=True, active=None):
    client = mock.MagicMock()
    client.read_inverter_telemetry.return_value = {
        "success": True, "config": {}, "telemetry": {"battery_installed": battery},
        "active_tou": {"enabled": True, "slots": active or []}}
    client.sync_tou_schedule.return_value = {"success": True}
    client.get_configured_inverter_ip.side_effect = lambda h: h
    client.get_configured_inverter_port.side_effect = lambda p: p
    return client


@pytest.mark.parametrize("field,value", [("power_w", "x"), ("power_w", [1]), ("target_soc", "50"),
                                         ("time", None), ("charge_current_a", {}), ("grid_charge", "yes")])
def test_manual_malformed_field_is_clean_fail(field, value):
    slots = _manual(_algo()["deye_tou_schedule"])
    slots[1][field] = value
    res = pstryk_engine.sync_manual_tou_schedule(json.dumps(slots), min_soc=P["min_soc"], max_soc=P["max_soc"],
                                                 dry_run=True)
    assert res["success"] is False and res["error"]


def test_manual_write_refused_without_auto_sync_off_flag():
    client = _client()
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        res = pstryk_engine.sync_manual_tou_schedule(json.dumps(_manual(_algo()["deye_tou_schedule"])),
                                                     host="127.0.0.1", port=1, min_soc=P["min_soc"], max_soc=P["max_soc"])
    assert res["success"] is False
    client.sync_tou_schedule.assert_not_called()


def test_manual_write_refused_without_battery():
    client = _client(battery=False)
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        res = pstryk_engine.sync_manual_tou_schedule(json.dumps(_manual(_algo()["deye_tou_schedule"])),
                                                     host="127.0.0.1", port=1, min_soc=P["min_soc"],
                                                     max_soc=P["max_soc"], auto_sync_off=True)
    assert res["success"] is False
    client.sync_tou_schedule.assert_not_called()


def test_manual_write_skipped_when_identical_unless_force():
    manual = _manual(_algo()["deye_tou_schedule"])
    dry = pstryk_engine.sync_manual_tou_schedule(json.dumps(manual), min_soc=P["min_soc"], max_soc=P["max_soc"],
                                                 dry_run=True)
    client = _client(active=dry["slots"])
    kw = dict(host="127.0.0.1", port=1, min_soc=P["min_soc"], max_soc=P["max_soc"], auto_sync_off=True)
    with mock.patch("pstryk_engine.solarman_v5_client", client):
        res = pstryk_engine.sync_manual_tou_schedule(json.dumps(manual), **kw)
        assert res["success"] is True and res.get("unchanged") is True
        client.sync_tou_schedule.assert_not_called()
        res = pstryk_engine.sync_manual_tou_schedule(json.dumps(manual), force=True, **kw)
    assert res["success"] is True
    client.sync_tou_schedule.assert_called_once()


def test_chat_refuses_non_https_base_url(monkeypatch):
    up = _Urlopen('{"slots": []}')
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", up)
    monkeypatch.setattr(energy_defaults, "AI_TOU_BASE_URL", "http://example.invalid/v1")
    content, err = ai_tou_planner._chat([{"role": "user", "content": "x"}], FAKE_KEY)
    assert content is None and err == "ai_insecure_base_url" and up.calls == 0


def test_chat_wall_clock_deadline(monkeypatch):
    import threading
    release = threading.Event()

    def hang(req, timeout=None):
        release.wait(5)
        raise OSError("late")
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", hang)
    monkeypatch.setattr(energy_defaults, "AI_TOU_TIMEOUT_S", 0.2)
    import time as _t
    t0 = _t.monotonic()
    content, err = ai_tou_planner._chat([{"role": "user", "content": "x"}], FAKE_KEY)
    release.set()
    assert content is None and err == "ai_call_failed: deadline" and _t.monotonic() - t0 < 2


def test_ai_skipped_when_sync_budget_exhausted(monkeypatch):
    up = _Urlopen({"slots": _ai_slots_from(_algo()["deye_tou_schedule"])})
    monkeypatch.setattr(ai_tou_planner.urllib.request, "urlopen", up)
    started = ai_tou_planner.time.monotonic() - float(energy_defaults.SHELL_COMMAND_TIMEOUT_S)
    plan = pstryk_engine.plan_tou(FRAMES, NOW, SOC, PROFILE, allow_ai_call=True, force_ai=True,
                                  sync_started=started, **P)
    assert plan["plan_source"] == "algorithm" and plan["fallback_reason"] == "ai_skipped_budget"
    assert up.calls == 0
