"""
Unit tests for Generic UI Pending Operations logic.
Tests JSON list serialization, parsing, addition, duplicate suppression,
removal, and state detection according to Home Assistant Jinja2 template logic.
"""

import json
import pytest


def jinja_set_ui_operation(current_raw: str, operation: str) -> str:
    """Python simulation of script.set_ui_operation_pending Jinja logic."""
    try:
        op_list = json.loads(current_raw) if current_raw and current_raw.startswith("[") else []
    except Exception:
        op_list = []
    if operation not in op_list:
        op_list.append(operation)
    return json.dumps(op_list)


def jinja_clear_ui_operation(current_raw: str, operation: str) -> str:
    """Python simulation of script.clear_ui_operation_pending Jinja logic."""
    try:
        op_list = json.loads(current_raw) if current_raw and current_raw.startswith("[") else []
    except Exception:
        op_list = []
    new_list = [x for x in op_list if x != operation]
    return json.dumps(new_list)


def jinja_is_any_pending(current_raw: str) -> bool:
    """Python simulation of binary_sensor.ui_any_operation_pending logic."""
    return current_raw not in ["[]", "", "unavailable", "unknown", None]


def jinja_is_operation_pending(current_raw: str, operation: str) -> bool:
    """Python simulation of binary_sensor.ui_op_pending_{operation} logic."""
    try:
        op_list = json.loads(current_raw) if current_raw and current_raw.startswith("[") else []
    except Exception:
        op_list = []
    return operation in op_list


def test_empty_initial_state():
    initial = "[]"
    assert not jinja_is_any_pending(initial)
    assert not jinja_is_operation_pending(initial, "deye_battery_safety")
    assert not jinja_is_operation_pending(initial, "deye_tou_sync")


def test_set_single_operation():
    current = "[]"
    updated = jinja_set_ui_operation(current, "deye_battery_safety")
    assert json.loads(updated) == ["deye_battery_safety"]
    assert jinja_is_any_pending(updated)
    assert jinja_is_operation_pending(updated, "deye_battery_safety")
    assert not jinja_is_operation_pending(updated, "deye_tou_sync")


def test_idempotent_duplicate_addition():
    current = json.dumps(["deye_battery_safety"])
    updated = jinja_set_ui_operation(current, "deye_battery_safety")
    assert json.loads(updated) == ["deye_battery_safety"]


def test_multiple_concurrent_operations():
    current = json.dumps(["deye_battery_safety"])
    updated = jinja_set_ui_operation(current, "deye_tou_sync")
    assert json.loads(updated) == ["deye_battery_safety", "deye_tou_sync"]
    assert jinja_is_any_pending(updated)
    assert jinja_is_operation_pending(updated, "deye_battery_safety")
    assert jinja_is_operation_pending(updated, "deye_tou_sync")


def test_clear_single_operation():
    current = json.dumps(["deye_battery_safety", "deye_tou_sync"])
    updated = jinja_clear_ui_operation(current, "deye_battery_safety")
    assert json.loads(updated) == ["deye_tou_sync"]
    assert jinja_is_any_pending(updated)
    assert not jinja_is_operation_pending(updated, "deye_battery_safety")
    assert jinja_is_operation_pending(updated, "deye_tou_sync")


def test_clear_last_operation():
    current = json.dumps(["deye_tou_sync"])
    updated = jinja_clear_ui_operation(current, "deye_tou_sync")
    assert json.loads(updated) == []
    assert not jinja_is_any_pending(updated)
    assert not jinja_is_operation_pending(updated, "deye_tou_sync")


def test_malformed_input_recovery():
    current = "malformed_json"
    updated = jinja_set_ui_operation(current, "deye_battery_safety")
    assert json.loads(updated) == ["deye_battery_safety"]

    cleared = jinja_clear_ui_operation("malformed_json", "deye_battery_safety")
    assert json.loads(cleared) == []
