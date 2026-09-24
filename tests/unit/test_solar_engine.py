#!/usr/bin/env python3
"""
Unit test suite for config/solar_engine.py.
Verifies astronomical solar calculations across seasons, boundary coordinates,
fail-safe fallback triggers, and solar phase classification.
"""

import sys
import os
import datetime
from zoneinfo import ZoneInfo
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
sys.path.insert(0, CONFIG_DIR)

import solar_engine


def test_seasonal_astronomical_calculations():
    """
    Verifies that sunrise and sunset calculations in Warsaw reflect
    dramatic seasonal changes (Winter short days vs Summer long days).
    """
    # Winter Solstice (Dec 21, 2026)
    winter = solar_engine.calculate_solar_data(target_date=datetime.date(2026, 12, 21))
    assert winter["status"] == "ok"
    assert winter["source"] == "astral"
    # In Warsaw, winter sunrise is ~07:40, sunset is ~15:28
    assert winter["today"]["sunrise"].startswith("07:3") or winter["today"]["sunrise"].startswith("07:4")
    assert winter["today"]["sunset"].startswith("15:2") or winter["today"]["sunset"].startswith("15:3")
    assert 7.0 <= winter["today"]["daylight_duration_hours"] <= 8.5

    # Summer Solstice (Jun 21, 2026)
    summer = solar_engine.calculate_solar_data(target_date=datetime.date(2026, 6, 21))
    assert summer["status"] == "ok"
    # In Warsaw, summer sunrise is ~04:10, sunset is ~21:05
    assert summer["today"]["sunrise"].startswith("04:0") or summer["today"]["sunrise"].startswith("04:1")
    assert summer["today"]["sunset"].startswith("21:0") or summer["today"]["sunset"].startswith("21:1")
    assert 16.0 <= summer["today"]["daylight_duration_hours"] <= 17.5


def test_outdoor_dusk_offset_calculation():
    """
    Verifies that outdoor dusk is earlier than sunset by the specified minutes.
    """
    data_30m = solar_engine.calculate_solar_data(
        target_date=datetime.date(2026, 9, 24),
        outdoor_offset_minutes=30
    )
    assert data_30m["status"] == "ok"
    sunset_str = data_30m["today"]["sunset"]
    outdoor_str = data_30m["today"]["outdoor_dusk"]

    # Parse HH:MM:SS
    sh, sm, ss = map(int, sunset_str.split(":"))
    oh, om, os_ = map(int, outdoor_str.split(":"))

    sunset_sec = sh * 3600 + sm * 60 + ss
    outdoor_sec = oh * 3600 + om * 60 + os_

    # Difference should be exactly 30 minutes (1800 seconds)
    assert (sunset_sec - outdoor_sec) == 1800


def test_invalid_coordinates_fallback():
    """
    CRITICAL SAFETY INVARIANT: If invalid latitude or longitude are provided,
    the engine must NOT crash. It must return status='fallback' with valid HH:MM:SS strings.
    """
    data_invalid_lat = solar_engine.calculate_solar_data(latitude=999.0)
    assert data_invalid_lat["status"] == "fallback"
    assert data_invalid_lat["source"] == "static_defaults"
    assert "Invalid coordinate" in data_invalid_lat["error"]
    assert data_invalid_lat["today"]["dawn"] == "06:00:00"
    assert data_invalid_lat["today"]["sunset"] == "18:00:00"
    assert data_invalid_lat["today"]["outdoor_dusk"] == "17:30:00"

    data_invalid_lon = solar_engine.calculate_solar_data(longitude=-250.0)
    assert data_invalid_lon["status"] == "fallback"
    assert data_invalid_lon["today"]["sunset"] == "18:00:00"


def test_solar_phase_classification_day_and_night():
    """
    Verifies solar phase classification at different times of day.
    """
    tz = ZoneInfo("Europe/Warsaw")
    # Noon simulation (bright daylight)
    noon_time = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=tz)
    data_noon = solar_engine.calculate_solar_data(ref_time=noon_time)
    assert data_noon["current"]["solar_phase"] == "daylight"
    assert data_noon["current"]["is_sun_up"] is True
    assert data_noon["current"]["is_dark_outside"] is False

    # Midnight simulation (deep night)
    midnight_time = datetime.datetime(2026, 9, 24, 1, 0, tzinfo=tz)
    data_midnight = solar_engine.calculate_solar_data(ref_time=midnight_time)
    assert data_midnight["current"]["solar_phase"] == "night"
    assert data_midnight["current"]["is_sun_up"] is False
    assert data_midnight["current"]["is_dark_outside"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
