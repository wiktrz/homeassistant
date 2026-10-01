#!/usr/bin/env python3
"""
Unit test suite for InPost Paczkomat MAR13M REST integration.
Tests verify:
- config/rest.yaml exists and parses as valid YAML.
- Resource URL, HTTP method, headers, and scan_interval (7200s / 2h).
- Existence and metadata of all 7 REST sensors.
- Jinja2 template execution against authentic MAR13M payload.
- Graceful fallback to 'unavailable' on missing, null, or malformed data.
"""

import os
import yaml
import pytest
import jinja2

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
REST_YAML_PATH = os.path.join(REPO_ROOT, "config", "rest.yaml")

REAL_MAR13M_PAYLOAD = {
    "message": "Data source: Drupal point_entity_field_data table",
    "air_index_level": "GOOD",
    "air_sensors": [
        "PM1:9.25:",
        "PM25:13.39:89.28",
        "PM10:18.72:41.59",
        "PRESSURE:1032.25:",
        "HUMIDITY:81.25:",
        "TEMPERATURE:13.91:",
    ],
}


def load_rest_config():
    assert os.path.exists(REST_YAML_PATH), f"File not found: {REST_YAML_PATH}"
    with open(REST_YAML_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, list), "rest.yaml root must be a list of REST resources"
    assert len(data) >= 1, "rest.yaml must contain at least one resource"
    return data


def test_rest_yaml_structure_and_scan_interval():
    """Verify config/rest.yaml resource URL, method, headers, and scan_interval."""
    configs = load_rest_config()
    inpost_res = None
    for res in configs:
        if "MAR13M" in res.get("resource", ""):
            inpost_res = res
            break

    assert inpost_res is not None, "InPost MAR13M resource not found in rest.yaml"
    assert inpost_res["resource"] == "https://inpost.pl/shipx-point-data/45898/MAR13M/air_index_level"
    assert inpost_res.get("method") == "POST"
    assert inpost_res.get("scan_interval") == 7200, "scan_interval must be exactly 7200 seconds"

    headers = inpost_res.get("headers", {})
    assert headers.get("X-Requested-With") == "XMLHttpRequest"
    assert "User-Agent" in headers
    assert "HomeAssistant" in headers["User-Agent"]


def test_sensor_definitions_and_metadata():
    """Verify all 7 sensors exist with appropriate device classes and units."""
    configs = load_rest_config()
    inpost_res = configs[0]
    sensors = inpost_res.get("sensor", [])
    assert len(sensors) == 7, f"Expected 7 sensors, got {len(sensors)}"

    sensors_by_id = {s.get("unique_id"): s for s in sensors}

    expected_sensors = {
        "paczkomat_mar13m_air_index_level": {
            "name": "Paczkomat MAR13M Air Index Level",
        },
        "paczkomat_mar13m_temperature": {
            "name": "Paczkomat MAR13M Temperature",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
        },
        "paczkomat_mar13m_humidity": {
            "name": "Paczkomat MAR13M Humidity",
            "device_class": "humidity",
            "unit_of_measurement": "%",
            "state_class": "measurement",
        },
        "paczkomat_mar13m_pressure": {
            "name": "Paczkomat MAR13M Pressure",
            "device_class": "pressure",
            "unit_of_measurement": "hPa",
            "state_class": "measurement",
        },
        "paczkomat_mar13m_pm25": {
            "name": "Paczkomat MAR13M PM25",
            "device_class": "pm25",
            "unit_of_measurement": "µg/m³",
            "state_class": "measurement",
        },
        "paczkomat_mar13m_pm10": {
            "name": "Paczkomat MAR13M PM10",
            "device_class": "pm10",
            "unit_of_measurement": "µg/m³",
            "state_class": "measurement",
        },
        "paczkomat_mar13m_pm1": {
            "name": "Paczkomat MAR13M PM1",
            "device_class": "pm1",
            "unit_of_measurement": "µg/m³",
            "state_class": "measurement",
        },
    }

    for unique_id, expected_props in expected_sensors.items():
        assert unique_id in sensors_by_id, f"Sensor {unique_id} missing from rest.yaml"
        sensor_def = sensors_by_id[unique_id]
        for prop, val in expected_props.items():
            assert sensor_def.get(prop) == val, (
                f"Mismatch for sensor {unique_id} prop {prop}: expected {val}, got {sensor_def.get(prop)}"
            )


def test_sensors_render_real_payload():
    """Verify all 7 sensors render the real MAR13M payload accurately."""
    configs = load_rest_config()
    sensors = configs[0].get("sensor", [])
    sensors_by_id = {s.get("unique_id"): s for s in sensors}

    env = jinja2.Environment()

    expected_values = {
        "paczkomat_mar13m_air_index_level": "GOOD",
        "paczkomat_mar13m_temperature": "13.91",
        "paczkomat_mar13m_humidity": "81.25",
        "paczkomat_mar13m_pressure": "1032.25",
        "paczkomat_mar13m_pm25": "13.39",
        "paczkomat_mar13m_pm10": "18.72",
        "paczkomat_mar13m_pm1": "9.25",
    }

    for unique_id, expected_val in expected_values.items():
        template_str = sensors_by_id[unique_id]["value_template"]
        tmpl = env.from_string(template_str)
        rendered = tmpl.render(value_json=REAL_MAR13M_PAYLOAD).strip()
        assert rendered == expected_val, (
            f"Sensor {unique_id} rendered '{rendered}', expected '{expected_val}'"
        )


@pytest.mark.parametrize(
    "malformed_payload",
    [
        {},
        {"air_sensors": []},
        {"air_sensors": None},
        {"air_sensors": "invalid_string"},
        {"air_sensors": ["TEMPERATURE:", "HUMIDITY::", "PRESSURE:   :"]},
        {"air_sensors": ["SOME_OTHER_SENSOR:12.3:"]},
        {"air_index_level": None},
        {"air_index_level": ""},
        {"air_index_level": "   "},
    ],
)
def test_sensors_graceful_unavailable_fallback(malformed_payload):
    """Verify all sensors output 'unavailable' on missing, null, or malformed payloads."""
    configs = load_rest_config()
    sensors = configs[0].get("sensor", [])
    env = jinja2.Environment()

    for s in sensors:
        tmpl = env.from_string(s["value_template"])
        rendered = tmpl.render(value_json=malformed_payload).strip()
        # For air_index_level, if air_index_level is not provided in malformed_payload, it should be unavailable
        # For numeric sensors, if their corresponding prefix is not valid in air_sensors, it should be unavailable
        if s["unique_id"] == "paczkomat_mar13m_air_index_level":
            if not malformed_payload.get("air_index_level") or str(malformed_payload.get("air_index_level")).strip() == "":
                assert rendered == "unavailable", f"{s['unique_id']} should be unavailable for {malformed_payload}"
        else:
            assert rendered == "unavailable", (
                f"{s['unique_id']} rendered '{rendered}' instead of 'unavailable' for {malformed_payload}"
            )
