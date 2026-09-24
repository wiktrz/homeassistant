#!/usr/bin/env python3
"""
Dynamic Solar & Astronomical Calculation Engine for Home Assistant.
Computes daily solar times (dawn, sunrise, solar noon, sunset, dusk, outdoor dusk),
solar elevation, azimuth, and real-time solar phase for Home Assistant automations.

Features:
- Exact astronomical calculation via Astral 2.2
- Resilient multi-tier fallback for missing coordinates, polar exceptions, or calculation errors
- Standalone CLI interface with JSON output for integration and unit testing
"""

import sys
import json
import argparse
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

DEFAULT_LATITUDE = 52.34453253634735
DEFAULT_LONGITUDE = 21.099288761615757
DEFAULT_ELEVATION = 322.0
DEFAULT_TIMEZONE = "Europe/Warsaw"
DEFAULT_OUTDOOR_OFFSET_MINUTES = 30

# Fail-safe static constants
FALLBACK_DAWN = "06:00:00"
FALLBACK_SUNRISE = "06:30:00"
FALLBACK_NOON = "12:00:00"
FALLBACK_SUNSET = "18:00:00"
FALLBACK_DUSK = "18:30:00"
FALLBACK_OUTDOOR_DUSK = "17:30:00"


def calculate_solar_data(
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
    elevation_m: float = DEFAULT_ELEVATION,
    tz_name: str = DEFAULT_TIMEZONE,
    target_date: Optional[datetime.date] = None,
    outdoor_offset_minutes: int = DEFAULT_OUTDOOR_OFFSET_MINUTES,
    ref_time: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Computes solar metrics for the given location and date with fail-safe fallback.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if target_date is None:
        target_date = datetime.datetime.now(tz).date()

    if ref_time is None:
        ref_time = datetime.datetime.now(tz)
    elif ref_time.tzinfo is None:
        ref_time = ref_time.replace(tzinfo=tz)

    try:
        # Validate coordinate boundaries
        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            raise ValueError(f"Invalid coordinate bounds: lat={latitude}, lon={longitude}")

        from astral import Observer
        from astral.sun import sun, elevation, azimuth

        obs = Observer(latitude=latitude, longitude=longitude, elevation=elevation_m)
        s = sun(obs, date=target_date, tzinfo=tz)

        dawn_dt = s.get("dawn")
        sunrise_dt = s.get("sunrise")
        noon_dt = s.get("noon")
        sunset_dt = s.get("sunset")
        dusk_dt = s.get("dusk")

        if not all([dawn_dt, sunrise_dt, sunset_dt]):
            raise ValueError("Astral returned incomplete solar events for date")

        # Outdoor dusk offset (e.g. 30 minutes before sunset)
        outdoor_dusk_dt = sunset_dt - datetime.timedelta(minutes=outdoor_offset_minutes)

        daylight_duration_seconds = (sunset_dt - sunrise_dt).total_seconds()
        daylight_hours = round(max(0.0, daylight_duration_seconds / 3600.0), 2)

        # Real-time elevation and azimuth
        try:
            curr_elevation = round(float(elevation(obs, ref_time)), 2)
            curr_azimuth = round(float(azimuth(obs, ref_time)), 2)
        except Exception:
            curr_elevation = 0.0
            curr_azimuth = 0.0

        # Phase determination
        # Elevation thresholds:
        # < -18: astronomical night
        # -18 to -12: astronomical twilight
        # -12 to -6: nautical twilight
        # -6 to -0.833: civil dawn / civil dusk
        # -0.833 to 6: sunrise / golden hour
        # > 6: daylight
        past_ref = ref_time - datetime.timedelta(minutes=10)
        is_rising = False
        try:
            past_elev = float(elevation(obs, past_ref))
            is_rising = curr_elevation >= past_elev
        except Exception:
            is_rising = curr_elevation > 0

        if curr_elevation < -18.0:
            solar_phase = "night"
        elif curr_elevation < -12.0:
            solar_phase = "astronomical_twilight"
        elif curr_elevation < -6.0:
            solar_phase = "nautical_twilight"
        elif curr_elevation < -0.833:
            solar_phase = "dawn" if is_rising else "dusk"
        elif curr_elevation < 6.0:
            solar_phase = "sunrise" if is_rising else "golden_hour"
        else:
            solar_phase = "daylight"

        is_dark_outside = curr_elevation <= -4.0 or ref_time >= outdoor_dusk_dt or ref_time < dawn_dt
        is_sun_up = curr_elevation > -0.833

        return {
            "date": target_date.isoformat(),
            "status": "ok",
            "source": "astral",
            "today": {
                "dawn": dawn_dt.strftime("%H:%M:%S"),
                "sunrise": sunrise_dt.strftime("%H:%M:%S"),
                "solar_noon": noon_dt.strftime("%H:%M:%S") if noon_dt else FALLBACK_NOON,
                "sunset": sunset_dt.strftime("%H:%M:%S"),
                "dusk": dusk_dt.strftime("%H:%M:%S") if dusk_dt else FALLBACK_DUSK,
                "outdoor_dusk": outdoor_dusk_dt.strftime("%H:%M:%S"),
                "daylight_duration_hours": daylight_hours,
            },
            "current": {
                "elevation": curr_elevation,
                "azimuth": curr_azimuth,
                "solar_phase": solar_phase,
                "is_dark_outside": is_dark_outside,
                "is_sun_up": is_sun_up,
            },
        }

    except Exception as exc:
        # Fail-safe Fallback Mode
        ref_time_str = ref_time.strftime("%H:%M:%S")
        is_dark_fallback = ref_time_str >= FALLBACK_OUTDOOR_DUSK or ref_time_str < FALLBACK_DAWN

        return {
            "date": target_date.isoformat(),
            "status": "fallback",
            "source": "static_defaults",
            "error": str(exc),
            "today": {
                "dawn": FALLBACK_DAWN,
                "sunrise": FALLBACK_SUNRISE,
                "solar_noon": FALLBACK_NOON,
                "sunset": FALLBACK_SUNSET,
                "dusk": FALLBACK_DUSK,
                "outdoor_dusk": FALLBACK_OUTDOOR_DUSK,
                "daylight_duration_hours": 12.0,
            },
            "current": {
                "elevation": 0.0,
                "azimuth": 0.0,
                "solar_phase": "night" if is_dark_fallback else "daylight",
                "is_dark_outside": is_dark_fallback,
                "is_sun_up": not is_dark_fallback,
            },
        }


def main():
    parser = argparse.ArgumentParser(description="Astronomical Solar Calculation Engine for Home Assistant")
    parser.add_argument("--lat", type=float, default=DEFAULT_LATITUDE, help="Observer latitude (degrees)")
    parser.add_argument("--lon", type=float, default=DEFAULT_LONGITUDE, help="Observer longitude (degrees)")
    parser.add_argument("--elevation", type=float, default=DEFAULT_ELEVATION, help="Observer elevation (meters)")
    parser.add_argument("--tz", type=str, default=DEFAULT_TIMEZONE, help="Timezone name")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD (defaults to today)")
    parser.add_argument("--outdoor-offset", type=int, default=DEFAULT_OUTDOOR_OFFSET_MINUTES, help="Outdoor dusk minutes before sunset")
    parser.add_argument("--json", action="store_true", default=True, help="Output JSON (default true)")

    args = parser.parse_args()

    target_date = None
    if args.date:
        try:
            target_date = datetime.date.fromisoformat(args.date)
        except ValueError:
            sys.stderr.write(f"Invalid date format: {args.date}, expected YYYY-MM-DD\n")
            sys.exit(1)

    result = calculate_solar_data(
        latitude=args.lat,
        longitude=args.lon,
        elevation_m=args.elevation,
        tz_name=args.tz,
        target_date=target_date,
        outdoor_offset_minutes=args.outdoor_offset,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
