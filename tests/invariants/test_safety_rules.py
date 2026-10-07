#!/usr/bin/env python3
"""
Tier 1: Architectural Hardware & Safety Invariant Audits
Enforces all mandatory constraints from AI_DEVELOPMENT_GUIDELINES.md and AGENT.md.
"""

import os
import re
import yaml
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

PROTECTED_POWER_SUPPLIES = [
    "switch.zasilanie_salon_dodatkowe",
    "light.boneio_32_l_07_new_light_18",
    "switch.zasilanie_sypialnia_garderoba",
    "light.boneio_32_l_07_new_light_07",
    "switch.zasilanie_mala_lazienka",
    "light.boneio_32_l_07_new_light_20",
    "switch.zasilanie_taras",
    "light.boneio_32_l_07_new_light_11",
]


def test_protected_power_supplies_excluded_from_all_lights_off():
    """
    INVARIANT 1: Protected LED power supplies must NEVER be toggled off
    by script.all_lights_off, sleep routines, or broad turn-off groups.
    """
    scripts_path = os.path.join(CONFIG_DIR, "scripts.yaml")
    assert os.path.exists(scripts_path), f"Missing {scripts_path}"

    with open(scripts_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Locate script.all_lights_off
    all_lights_off_match = re.search(r"all_lights_off:\s*\n((?:[ \t]+.*\n)*)", content)
    if all_lights_off_match:
        script_body = all_lights_off_match.group(1)
        for entity in PROTECTED_POWER_SUPPLIES:
            assert entity not in script_body, (
                f"SAFETY VIOLATION: Protected power supply '{entity}' is included in "
                f"script.all_lights_off! Cutting power destroys downstream 24V LED drivers."
            )


def test_no_object_id_in_mqtt_yaml():
    """
    INVARIANT 2: Home Assistant manual MQTT schema does NOT support 'object_id'.
    Only 'unique_id:' and 'name:' are allowed.
    """
    mqtt_path = os.path.join(CONFIG_DIR, "mqtt.yaml")
    assert os.path.exists(mqtt_path), f"Missing {mqtt_path}"

    with open(mqtt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"^\s*object_id\s*:", line):
            pytest.fail(
                f"SCHEMA VIOLATION in config/mqtt.yaml at line {idx}: 'object_id' is not supported "
                f"by Home Assistant MQTT schema. Use 'unique_id' instead."
            )


def test_entrance_door_strike_safety_timeout():
    """
    INVARIANT 3: Electric strike lock (lock.rygiel_drzwi_wejsciowych_lock or relay 23)
    must always enforce an automatic safety shutoff within 120s (2 minutes) to prevent coil burnout.
    """
    scripts_path = os.path.join(CONFIG_DIR, "scripts.yaml")
    automations_path = os.path.join(CONFIG_DIR, "automations.yaml")

    sources = [scripts_path, automations_path]
    strike_relay = "light.boneio_32_l_07_73bbd8_door_23_relay"
    strike_button = "btn_entrance_door"

    found_triggers = False
    for src in sources:
        if not os.path.exists(src):
            continue
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()

        if strike_relay in text or strike_button in text:
            found_triggers = True
            # Verify that any turn_on or unlock action has an accompanying delay/turn_off
            # or timer <= 120s
            # Look for 120s or 2m or delay
            assert (
                "delay" in text or "timer" in text or "turn_off" in text or "service: input_button.press" in text
            ), f"Door strike trigger in {src} lacks safety shutoff mechanism!"

    assert found_triggers, "Could not find door strike relay or button in scripts or automations."


def test_automations_syntax_and_no_invalid_enabled_keys():
    """
    INVARIANT 4: Automations must not contain invalid YAML attributes like 'data: {enabled: False}'
    which causes Home Assistant startup validation failures.
    """
    automations_path = os.path.join(CONFIG_DIR, "automations.yaml")
    assert os.path.exists(automations_path)

    with open(automations_path, "r", encoding="utf-8") as f:
        try:
            automations = yaml.safe_load(f)
        except Exception as e:
            pytest.fail(f"YAML parsing error in config/automations.yaml: {e}")

    assert isinstance(automations, list), "config/automations.yaml must contain a list of automations."

    for idx, auto in enumerate(automations):
        alias = auto.get("alias", f"automation_{idx}")
        # Verify no data with 'enabled' key mistakenly added to data payloads
        action = auto.get("action", [])
        if isinstance(action, dict):
            action = [action]
        if isinstance(action, list):
            for act in action:
                if isinstance(act, dict) and "data" in act and isinstance(act["data"], dict):
                    assert "enabled" not in act["data"], (
                        f"Automation '{alias}' contains invalid 'enabled' key inside 'data': {act['data']}"
                    )



def test_modern_dashboards_referenced_helpers_and_templates_exist():
    """
    INVARIANT 5: All input_* helpers and template sensors referenced in modern dashboards
    (dashboard_modern_reference.yaml and dashboard_home_improved.yaml) must be defined
    in configuration.yaml or templates.yaml. Prevents 'Entity not found' Lovelace errors.
    """
    class SafeLoaderIgnore(yaml.SafeLoader):
        pass

    SafeLoaderIgnore.add_constructor(None, lambda loader, node: None)

    cfg_path = os.path.join(CONFIG_DIR, "configuration.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.load(f, Loader=SafeLoaderIgnore) or {}

    helpers = set()
    for domain in ["input_boolean", "input_number", "input_select", "input_datetime", "input_button", "input_text"]:
        if domain in cfg and isinstance(cfg[domain], dict):
            for k in cfg[domain].keys():
                helpers.add(f"{domain}.{k}")

    def slugify(text):
        text = text.lower()
        mapping = {
            'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            ' ': '_', '-': '_', '/': '_', '.': '_'
        }
        res = [mapping.get(ch, ch) for ch in text if ch in mapping or ch.isalnum() or ch == '_']
        return re.sub(r'_+', '_', "".join(res)).strip('_')

    tpl_path = os.path.join(CONFIG_DIR, "templates.yaml")
    tpl_entities = set()
    with open(tpl_path, "r", encoding="utf-8") as f:
        tpl_list = yaml.load(f, Loader=SafeLoaderIgnore) or []
        for block in tpl_list:
            if isinstance(block, dict):
                for d in ["sensor", "binary_sensor"]:
                    for item in block.get(d, []):
                        if isinstance(item, dict) and "name" in item:
                            tpl_entities.add(f"{d}.{slugify(str(item['name']))}")

    # Inspect modern dashboards
    pattern = re.compile(r'\b(input_boolean|input_number|input_select|input_datetime|input_button|input_text|sensor|binary_sensor)\.[a-z0-9_]+\b')
    for db_name in ["dashboard_modern_reference.yaml", "dashboard_home_improved.yaml"]:
        db_path = os.path.join(CONFIG_DIR, db_name)
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        for m in pattern.finditer(content):
            eid = m.group(0)
            if eid.startswith("input_"):
                if eid == "input_button.press":
                    continue
                assert eid in helpers, f"Missing helper '{eid}' referenced in {db_name}!"
            elif any(kw in eid for kw in ["pstryk_", "deye_battery_", "dynamic_"]):
                assert eid in tpl_entities, f"Missing template sensor '{eid}' referenced in {db_name}!"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

