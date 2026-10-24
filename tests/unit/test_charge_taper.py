"""
TDD: SOC-aware charge rate (taper), history fit and floor-aware hourly Reg 108 currents (plan items 1-3).
Hermetic: no network, the history cache lives in tmp_path.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config"))
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
import deye_cloud_client  # noqa: E402
import energy_defaults  # noqa: E402
import pstryk_engine as pe  # noqa: E402

W, UTC, HOUR = pe.WARSAW_TZ, timezone.utc, timedelta(hours=1)
CAP = energy_defaults.FALLBACK_BATTERY_CAPACITY_KWH
VOLTS = energy_defaults.FALLBACK_BATTERY_NOMINAL_VOLTAGE
CEIL = energy_defaults.BATTERY_CHARGE_CURRENT_MAX_CEILING_A
FLOOR = int(round(energy_defaults.BATTERY_CHARGE_CURRENT_MIN_FLOOR_A))
LOSS = energy_defaults.DEFAULT_LOSS_BUFFER_PCT
P = dict(min_soc=10, max_soc=100, capacity_kwh=CAP, voltage=VOLTS, ceiling_a=CEIL, loss_pct=LOSS)
SUN = {6: .5061, 7: .2799, 8: .2591, 9: .2443, 10: .2571, 11: .2592, 12: .2604, 13: .3577}
NOW_SUN = datetime.fromisoformat("2026-10-25T06:00:00+01:00")
DEFAULT = {"knee": energy_defaults.BATTERY_CHARGE_TAPER_KNEE_SOC, "end_fraction": energy_defaults.BATTERY_CHARGE_TAPER_END_FRACTION}


@pytest.fixture(autouse=True)
def _no_real_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DEYE_LOAD_HISTORY_FILE", str(tmp_path / "hist.json"))


def _sun_frames():
    t = datetime(2026, 10, 25, tzinfo=W).astimezone(UTC)
    end = datetime(2026, 10, 27, tzinfo=W).astimezone(UTC)
    out = []
    while t < end:
        out.append({"start": pe._iso_z(t), "end": pe._iso_z(t + HOUR),
                    "metrics": {"pricing": {"price_gross": SUN.get(t.astimezone(W).hour, 0.6)}}})
        t += HOUR
    return out


# ---- curve ----
def test_curve_shape():
    f = lambda s: pe.max_charge_current_at(s, 100.0, DEFAULT)
    assert f(0) == f(50) == f(DEFAULT["knee"]) == 100.0
    assert f(100) == pytest.approx(100.0 * DEFAULT["end_fraction"])
    mid = (DEFAULT["knee"] + 100) / 2
    assert f(mid) == pytest.approx(100.0 * (1 + DEFAULT["end_fraction"]) / 2)
    assert pe.max_charge_current_at(95, 80.0, None) == pytest.approx(80.0 * (1 + DEFAULT["end_fraction"]) / 2)  # env default


def test_projection_from_10_takes_longer_than_linear():
    rate = CEIL * VOLTS / 1000.0
    a = datetime(2026, 10, 25, 0, tzinfo=W)
    cell = lambda hrs: [{"start": a, "end": a + timedelta(hours=hrs), "target_soc": 100, "grid_charge": True}]
    kwargs = dict(min_soc=10, max_soc=100, capacity_kwh=CAP, charge_kw=rate)
    linear_h = (90 / 100.0 * CAP) / rate
    still = pe.project_soc_timeline(cell(linear_h), 10, 0.0, a, **kwargs)[0]["soc_end"]
    assert 90 < still < 100  # the linear estimate no longer reaches 100 %
    # below the knee nothing changes
    low = pe.project_soc_timeline(cell(1.0), 10, 0.0, a, **kwargs)[0]["soc_end"]
    assert low == pytest.approx(10 + rate * 100.0 / CAP)
    assert pe.project_soc_timeline(cell(linear_h * 3), 10, 0.0, a, **kwargs)[0]["soc_end"] == pytest.approx(100.0)


def test_planned_low_rate_is_not_slowed_by_the_taper():
    a = datetime(2026, 10, 25, 0, tzinfo=W)
    low = 1.0  # kW, far below the tapered ceiling even at 99 %
    s = [{"start": a, "end": a + HOUR, "target_soc": 100, "grid_charge": True, "charge_kw": low}]
    out = pe.project_soc_timeline(s, 92, 0.0, a, min_soc=10, max_soc=100, capacity_kwh=CAP, charge_kw=CEIL * VOLTS / 1000.0)
    assert out[0]["soc_end"] == pytest.approx(92 + low * 100.0 / CAP)


def test_energy_min_hours_grows_with_the_taper():
    base = pe.charge_energy_min_hours(10, 100, CAP, VOLTS, CEIL, LOSS)
    need = 0.9 * CAP * (1 + LOSS / 100.0) / (CEIL * VOLTS / 1000.0)
    assert base >= need  # never below the constant-rate estimate
    assert pe.charge_energy_min_hours(10, 100, CAP * 6, VOLTS, CEIL, LOSS) > (0.9 * CAP * 6 * (1 + LOSS / 100.0)) / (CEIL * VOLTS / 1000.0) + 0.5


# ---- fit ----
def _synth(knee, end, n_per_bin=5, noise=True):
    frames, t = [], datetime(2026, 10, 1, tzinfo=UTC)
    for soc in range(80, 101):
        f = 1.0 if soc <= knee else 1.0 - (1.0 - end) * (soc - knee) / (100.0 - knee)
        for k in range(n_per_bin):
            frac = f * (0.5 if noise and k else 1.0)  # lower readings below the envelope
            frames.append({"ts": t, "soc": float(soc), "battery_w": -frac * CEIL * VOLTS})
            t += timedelta(minutes=5)
    frames.append({"ts": t, "soc": 95.0, "battery_w": 3000.0})  # discharge frames are ignored
    return frames


def test_fit_works_with_positive_charge_sign():
    fr = [dict(f, battery_w=-f["battery_w"]) for f in _synth(92, 0.3)[:-1]]
    fit = pe.fit_charge_taper(fr)
    assert fit["source"] == "fitted" and abs(fit["knee"] - 92) <= 1.5


def test_fit_rejects_price_modulated_current_below_the_knee():
    fr = [dict(f, battery_w=f["battery_w"] * 0.3) if f["soc"] <= 90 else f for f in _synth(92, 0.3)[:-1]]
    assert pe.fit_charge_taper(fr)["source"] == "default"


def test_fit_uses_given_voltage_and_ceiling():
    fr = _synth(92, 0.3)
    assert pe.fit_charge_taper(fr, voltage=VOLTS, ceiling_a=CEIL)["source"] == "fitted"
    assert pe.fit_charge_taper(fr, voltage=VOLTS * 2, ceiling_a=CEIL)["source"] == "default"  # 50 % envelope below knee


def test_charge_walk_rejects_non_finite():
    for kw in ({"rate_kw": float("nan")}, {"rate_kw": 1.0, "ceiling_kw": float("inf")}):
        a = dict(rate_kw=1.0, ceiling_kw=None); a.update(kw)
        with pytest.raises(ValueError):
            pe._charge_walk(50, a["rate_kw"], CAP, DEFAULT, hours=1.0, ceiling_kw=a["ceiling_kw"])


def test_fit_recovers_known_knee():
    fit = pe.fit_charge_taper(_synth(92, 0.3))
    assert fit["source"] == "fitted"
    assert abs(fit["knee"] - 92) <= 1.5 and abs(fit["end_fraction"] - 0.3) <= 0.06


def test_fit_with_too_few_samples_is_the_default():
    fit = pe.fit_charge_taper(_synth(92, 0.3, n_per_bin=1)[:60])
    assert fit == dict(DEFAULT, source="default")
    assert pe.fit_charge_taper([])["source"] == "default"


def test_engine_reads_the_fit_from_the_cache_and_defaults_without():
    assert pe.load_charge_taper() == dict(DEFAULT, source="default")
    path = os.environ["DEYE_LOAD_HISTORY_FILE"]
    json.dump({"frames": [], "charge_taper": {"knee": 93.0, "end_fraction": 0.4, "source": "fitted"}}, open(path, "w"))
    assert pe.load_charge_taper() == {"knee": 93.0, "end_fraction": 0.4, "source": "fitted"}
    assert pe.max_charge_current_at(100, 100.0) == pytest.approx(40.0)


def test_history_job_stores_the_fit_and_keeps_other_cache_keys(monkeypatch, tmp_path):
    path = str(tmp_path / "hist.json")
    json.dump({"fetched_date": "2020-01-01", "variant": "startAt", "custom_keep": {"a": 1}}, open(path, "w"))
    frames = _synth(92, 0.3)
    monkeypatch.setattr(deye_cloud_client, "list_stations", lambda cfg=None: [{"id": 1}])
    monkeypatch.setattr(deye_cloud_client, "fetch_station_history_day",
                        lambda sid, day, cfg=None, state=None: frames if day == date.today() - timedelta(days=1) else None)
    deye_cloud_client.fetch_consumption_history(days=1, cache_path=path)
    c = json.load(open(path))
    assert c["custom_keep"] == {"a": 1} and c["frames"] and c["charge_taper"]["source"] == "fitted"


# ---- floor-aware currents ----
def _window():
    return pe.find_charge_window(_sun_frames(), NOW_SUN)[0]


def _by_hour(amps):
    return {datetime.fromisoformat(k.replace("Z", "+00:00")).astimezone(W).hour: a for k, a in amps.items()}


def _stored(amps, soc):
    for h in sorted(amps):
        soc = pe._charge_walk(soc, amps[h] * VOLTS / 1000.0 * (1 - LOSS / 100.0), CAP, DEFAULT, hours=1.0, stop_soc=100,
                              ceiling_kw=CEIL * VOLTS / 1000.0 * (1 - LOSS / 100.0))[0]
    return soc


def test_soc_85_subtracts_dropped_floor_energy_and_does_not_overshoot():
    amps = _by_hour(pe.charge_current_plan(_window(), 85, 100, CAP, VOLTS, LOSS, CEIL))
    assert set(amps) == {7, 8, 9, 10, 11, 12} and all(a >= FLOOR for a in amps.values())
    assert amps[9] == max(amps.values())
    assert _stored(amps, 85.0) >= 99.5
    # the 15 % need is ~2.8 kWh = under 3 floor hours: no kept hour may be inflated above the floor
    assert max(amps.values()) == FLOOR
    soc = 85.0
    for h in sorted(amps):  # the plan really fills the battery (taper-aware)
        soc = pe._charge_walk(soc, amps[h] * VOLTS / 1000.0 * (1 - LOSS / 100.0), CAP, DEFAULT, hours=1.0, stop_soc=100,
                                ceiling_kw=CEIL * VOLTS / 1000.0 * (1 - LOSS / 100.0))[0]
    assert soc >= 99.5


def test_soc_10_reaches_max_with_taper_deficit_moved_earlier():
    amps = _by_hour(pe.charge_current_plan(_window(), 10, 100, CAP, VOLTS, LOSS, CEIL))
    assert all(FLOOR <= a <= CEIL for a in amps.values())
    soc = 10.0
    for h in sorted(amps):
        soc = pe._charge_walk(soc, amps[h] * VOLTS / 1000.0 * (1 - LOSS / 100.0), CAP, DEFAULT, hours=1.0, stop_soc=100,
                                ceiling_kw=CEIL * VOLTS / 1000.0 * (1 - LOSS / 100.0))[0]
    assert soc >= 99.5


def test_plan_exposes_taper_source_and_keeps_utc_keys():
    plan = pe.plan_rolling_tou(_sun_frames(), NOW_SUN, 85, None, **P)
    assert plan["charge_taper_source"] == "default"
    assert plan["charge_current_plan"] and all(k.endswith("Z") for k in plan["charge_current_plan"])
    assert plan["charge_windows"]


def test_history_job_logs_fit_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(deye_cloud_client, "list_stations", lambda cfg=None: [{"id": 1}])
    monkeypatch.setattr(deye_cloud_client, "fetch_station_history_day",
                        lambda sid, day, cfg=None, state=None: _synth(92, 0.3) if day == date.today() - timedelta(days=1) else None)
    monkeypatch.setattr(pe, "fit_charge_taper", lambda *a, **k: 1 / 0)
    deye_cloud_client.fetch_consumption_history(days=1, cache_path=str(tmp_path / "h.json"))
    assert "charge taper fit failed" in capsys.readouterr().err
