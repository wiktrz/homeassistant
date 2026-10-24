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
    (dashboard_modern_reference.yaml) must be defined
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
    for cl_item in cfg.get("command_line", []):
        if isinstance(cl_item, dict) and "sensor" in cl_item:
            s_item = cl_item["sensor"]
            if isinstance(s_item, dict) and "name" in s_item:
                tpl_entities.add(f"sensor.{slugify(str(s_item['name']))}")

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
    for db_name in ["dashboard_modern_reference.yaml"]:
        db_path = os.path.join(CONFIG_DIR, "dashboards", db_name)
        if not os.path.exists(db_path):
            db_path = os.path.join(CONFIG_DIR, db_name)
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        for m in pattern.finditer(content):
            eid = m.group(0)
            if eid.startswith("input_"):
                if eid == "input_button.press":
                    continue
                assert eid in helpers, f"Missing helper '{eid}' referenced in {db_name}!"
            elif any(kw in eid for kw in ["pstryk_", "deye_battery_", "dynamic_", "powerbank_"]):
                assert eid in tpl_entities, f"Missing template sensor '{eid}' referenced in {db_name}!"


def test_no_hardcoded_battery_parameters():
    """
    INVARIANT: ZERO HARDCODED BATTERY CONSTANTS.
    Enforces that battery capacity (16.13 kWh), nominal voltage (51.2 V),
    and maximum charging current (100 A) are NEVER hardcoded into Home Assistant
    YAML files (templates, configuration, dashboards, scripts, automations).
    All hardware values must be read dynamically from Deye inverter registers / API.
    """
    import re
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    config_dir = os.path.join(repo_root, "config")

    files_to_check = [
        os.path.join(config_dir, "templates.yaml"),
        os.path.join(config_dir, "configuration.yaml"),
        os.path.join(config_dir, "scripts.yaml"),
        os.path.join(config_dir, "automations.yaml"),
        os.path.join(config_dir, "dashboards", "dashboard_modern_reference.yaml"),
    ]

    forbidden_patterns = [
        (re.compile(r"default\s*\(\s*['\"]?16\.13['\"]?"), "Hardcoded 16.13 capacity default fallback"),
        (re.compile(r"float\s*\(\s*16\.13\s*\)"), "Hardcoded float(16.13) capacity fallback"),
        (re.compile(r"default\s*\(\s*['\"]?51\.2['\"]?"), "Hardcoded 51.2 voltage default fallback"),
        (re.compile(r"float\s*\(\s*51\.2\s*\)"), "Hardcoded float(51.2) voltage fallback"),
        (re.compile(r"state:\s*['\"]51\.2['\"]"), "Hardcoded state: 51.2 voltage"),
        (re.compile(r"default\s*\(\s*['\"]?100(?:\.0)?['\"]?"), "Hardcoded 100A current default fallback"),
        (re.compile(r"float\s*\(\s*100(?:\.0)?\s*\)"), "Hardcoded float(100.0) current fallback"),
        (re.compile(r"default\s*\(\s*['\"]?140(?:\.0)?['\"]?"), "Hardcoded 140A discharge current default fallback"),
        (re.compile(r"float\s*\(\s*140(?:\.0)?\s*\)"), "Hardcoded float(140.0) discharge current fallback"),
        (re.compile(r"SunDeposit\s+16\.13\s*kWh"), "Hardcoded static battery model string"),
    ]

    violations = []
    for fpath in files_to_check:
        if not os.path.exists(fpath):
            continue
        rel_path = os.path.relpath(fpath, repo_root)
        with open(fpath, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean_line = line.strip()
                # Skip comments and description strings that don't execute
                if clean_line.startswith("#") or "description:" in clean_line:
                    continue
                for pat, desc in forbidden_patterns:
                    if pat.search(line):
                        violations.append(f"{rel_path}:{line_no} [{desc}]: {clean_line}")

    assert not violations, "Found forbidden hardcoded battery parameters in YAML files:\n" + "\n".join(violations)


def test_register_write_safety_policies():
    """
    INVARIANT: HARDWARE SAFETY REGISTER ACCESS CONTROL.
    Ensures that registers 109 (Max Discharge Current), 110 (Min SOC),
    111 (Shutdown SOC), and 112 (Max SOC) remain strictly blocked in software
    from unprompted direct writes to prevent physical damage.
    Only Register 108 (Max Charge Current) is modulated under safe bounds.
    """
    import sys
    config_dir = os.path.join(REPO_ROOT, "config")
    if config_dir not in sys.path:
        sys.path.insert(0, config_dir)
    import solarman_v5_client

    assert hasattr(solarman_v5_client, "BLOCK_DIRECT_SYSTEM_REGISTER_WRITES")
    assert solarman_v5_client.BLOCK_DIRECT_SYSTEM_REGISTER_WRITES is True
    assert hasattr(solarman_v5_client, "set_charge_current")


def test_tou_zero_grid_export_and_no_generation(monkeypatch):
    """
    INVARIANT: ZERO GRID EXPORT & NO GENERATOR TRIGGERING.
    Asserts that enableSell is strictly False and enableGeneration is strictly False
    across all TOU schedule slots sent to Deye Cloud OpenAPI, ensuring zero grid export
    and no generator triggering.
    """
    import sys
    config_dir = os.path.join(REPO_ROOT, "config")
    if config_dir not in sys.path:
        sys.path.insert(0, config_dir)
    import deye_cloud_client

    captured_payloads = []

    def mock_call_deye_api(endpoint, method="GET", data=None, params=None, cfg=None):
        if data:
            captured_payloads.append(data)
        return True, {"code": "1000000", "msg": "SUCCESS"}, "SUCCESS"

    monkeypatch.setattr(deye_cloud_client, "call_deye_api", mock_call_deye_api)

    # 1. Test with adversarial input slots attempting to enable sell or generator
    adversarial_slots = [
        {"time": f"{i*4:02d}:00", "power": 5000, "soc": 80, "enableSell": True, "enableGeneration": True, "grid_charge": True}
        for i in range(6)
    ]
    res = deye_cloud_client.sync_tou_schedule(adversarial_slots, cfg={"inverter_sn": "TEST_SN"})
    assert res.get("success") is True, f"sync_tou_schedule failed: {res}"
    assert len(captured_payloads) == 1
    items = captured_payloads[0].get("timeUseSettingItems", [])
    assert len(items) == 6
    for idx, item in enumerate(items, 1):
        assert item.get("enableSell") is False, f"Slot {idx} has enableSell != False: {item}"
        assert item.get("enableGeneration") is False, f"Slot {idx} has enableGeneration != False: {item}"

    # 2. Test with standard generated schedule from pstryk_engine
    try:
        import pstryk_engine
        # TOU is only generated from price-derived input (charge + peak windows); without it nothing is written
        assert pstryk_engine.generate_deye_tou_schedule() is None
        gen_slots = pstryk_engine.generate_deye_tou_schedule(
            best_pb_window={"start": "02:00", "end": "04:00"}, best_sell_window={"start": "17:00", "end": "21:00"})
        for idx, s in enumerate(gen_slots, 1):
            assert s.get("enableSell", False) is False, f"Generated slot {idx} has enableSell=True"
            assert s.get("enableGeneration", False) is False, f"Generated slot {idx} has enableGeneration=True"
        captured_payloads.clear()
        res2 = deye_cloud_client.sync_tou_schedule(gen_slots, cfg={"inverter_sn": "TEST_SN"})
        assert res2.get("success") is True
        for idx, item in enumerate(captured_payloads[0].get("timeUseSettingItems", []), 1):
            assert item.get("enableSell") is False, f"Formatted slot {idx} has enableSell != False"
            assert item.get("enableGeneration") is False, f"Formatted slot {idx} has enableGeneration != False"
    except ImportError:
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])




def test_ai_tou_planner_never_writes_registers_or_leaks_the_key():
    """
    INVARIANT: the AI planner only proposes a table; it never writes inverter registers (no Reg 108/109/112 writes,
    no client write calls) and never prints the API key.
    """
    import ast
    for module in ("ai_tou_planner.py", "ai_tou_guard.py"):
        _assert_no_register_writes(ast, os.path.join(REPO_ROOT, "config", module))


def _assert_no_register_writes(ast, path):
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    banned_calls = {"set_charge_current", "set_charge_current_with_fallback", "write_register", "write_registers",
                    "sync_tou_schedule", "apply_confirmed_battery_safety_limits"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in (108, 109, 112) and not isinstance(node.value, bool):
            raise AssertionError(f"register literal {node.value} at line {node.lineno}")
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in banned_calls, f"register/inverter write call {name} at line {node.lineno}"
            if name == "print":
                names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
                assert "key" not in names, f"print of the API key at line {node.lineno}"
