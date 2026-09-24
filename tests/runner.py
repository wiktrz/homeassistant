#!/usr/bin/env python3
"""
Autonomous AI Test Harness & Test Catalog Runner.
Master CLI for testing Home Assistant configurations across all 4 tiers:
  Tier 0: Static Schema & Syntax (check_config in one-shot Docker)
  Tier 1: Hardware Safety & Invariant Audits (pytest invariant rules)
  Tier 2: Dynamic Home Assistant State & API Integration (ephemeral Docker)
  Tier 3: Lovelace Dashboard UI Structure & Entity Binding Validator

Features:
  --auto:      Inspects git diff, detects affected rooms/areas, runs targeted tests
  --tier <N>:  Runs specific tier (0, 1, 2, 3, or 'all')
  --area <A>:  Runs tests for a specific place/room (e.g., salon, kuchnia, media)
  --discover:  Detects test coverage gaps across scripts and automations
  --generate:  Self-building: generates draft test scenarios for untested features
  --keep-up:   Keeps test container running for repeated fast iterations
"""

import os
import sys
import argparse
import subprocess
import time
import re
import yaml
from datetime import datetime
from typing import Dict, Any, List, Set, Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
REGISTRY_FILE = os.path.join(TESTS_DIR, "test_registry.yaml")
SCENARIOS_DIR = os.path.join(TESTS_DIR, "scenarios")

sys.path.insert(0, TESTS_DIR)
import sandbox
from state_runner import HomeAssistantClient, execute_scenario
import ui_validator

# Area mapping heuristics based on PLACES_AND_BEHAVIORS.md
AREA_KEYWORDS = {
    "salon": ["salon", "kino", "tv", "remote.salon_2", "light.salon_mqtt"],
    "multimedia": ["radio", "voice", "streaming", "media_player", "spotify", "netflix"],
    "kuchnia": ["kuchnia", "kitchen", "wyspa", "jadalnia", "light.boneio_32_l_07_new_light_05"],
    "termostaty": ["termostat", "thermostat", "routine", "heating", "local01", "local02", "local03"],
    "bezpieczenstwo": ["zasilanie", "rygiel", "door_strike", "alarm", "power_supply"],
    "pstryk": ["pstryk", "energy", "price", "charging", "best_window"],
}


def load_registry() -> Dict[str, Any]:
    if not os.path.exists(REGISTRY_FILE):
        return {"version": "1.0", "tests": []}
    with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"version": "1.0", "tests": []}


def save_registry(registry_data: Dict[str, Any]):
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        yaml.dump(registry_data, f, sort_keys=False, indent=2, allow_unicode=True)


def run_tier0() -> bool:
    print("\n🔍 === [TIER 0: Static Schema & Syntax Check] ===")
    sandbox.create_sandbox()
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{sandbox.SANDBOX_DIR}:/config",
        "ghcr.io/home-assistant/home-assistant:2026.1.3",
        "python", "-m", "homeassistant", "--config", "/config", "--script", "check_config"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if "Incorrect config" in res.stdout or res.returncode != 0:
        print("❌ Tier 0 FAILED:")
        for line in res.stdout.splitlines():
            if "ERROR" in line or "Incorrect" in line:
                print(f"  {line}")
        return False
    print("✓ Tier 0 PASSED: Configuration schema & syntax valid.")
    return True


def run_tier1() -> bool:
    print("\n🛡️  === [TIER 1: Hardware Safety Invariant Audits] ===")
    cmd = [sys.executable, "-m", "pytest", os.path.join(TESTS_DIR, "invariants", "test_safety_rules.py"), "-v"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    for line in res.stdout.splitlines():
        if "PASSED" in line or "FAILED" in line:
            print(f"  {line}")
    if res.returncode != 0:
        print("❌ Tier 1 FAILED: Safety invariants breached!")
        return False
    print("✓ Tier 1 PASSED: All hardware invariants satisfied.")
    return True


def ensure_ha_running(timeout: int = 30) -> HomeAssistantClient:
    client = HomeAssistantClient()
    if client.is_healthy():
        return client

    print("🚀 Starting ephemeral Home Assistant test stack...")
    sandbox.create_sandbox()
    sandbox.start_containers()
    if not sandbox.wait_for_ha_ready(timeout_seconds=timeout):
        raise RuntimeError("Failed to start Home Assistant test instance within timeout.")
    return client


def run_tier2(area_filter: Optional[str] = None) -> bool:
    print("\n⚡ === [TIER 2: Dynamic Home Assistant State & Integration] ===")
    try:
        client = ensure_ha_running()
    except Exception as e:
        print(f"❌ Could not initialize test container: {e}")
        return False

    registry = load_registry()
    tests = registry.get("tests", [])

    if area_filter:
        tests = [t for t in tests if t.get("area") == area_filter]
        print(f"Filtering tests for area: '{area_filter}' ({len(tests)} test(s))")

    if not tests:
        print("No test scenarios found matching filter.")
        return True

    all_passed = True
    passed_count = 0

    for test in tests:
        rel_path = test.get("file")
        scenario_file = os.path.join(TESTS_DIR, rel_path)
        if not os.path.exists(scenario_file):
            print(f"⚠️  Missing scenario file: {scenario_file}")
            all_passed = False
            continue

        success, logs = execute_scenario(scenario_file, client)
        for line in logs:
            print(line)

        if success:
            passed_count += 1
            test["last_verified"] = datetime.now().isoformat()
        else:
            all_passed = False
        print()

    save_registry(registry)
    print(f"📊 Tier 2 Results: {passed_count}/{len(tests)} passed.")
    return all_passed


def run_tier3() -> bool:
    print("\n🖥️  === [TIER 3: Lovelace Dashboard UI Validator] ===")
    return ui_validator.run_all_dashboard_validations()


def detect_git_impacted_areas() -> Set[str]:
    """Inspects git diff to identify impacted functional areas."""
    cmd = ["git", "status", "--porcelain"]
    status_res = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    
    cmd_diff = ["git", "diff", "HEAD"]
    diff_res = subprocess.run(cmd_diff, capture_output=True, text=True, cwd=REPO_ROOT)

    combined_text = (status_res.stdout + "\n" + diff_res.stdout).lower()
    impacted = set()

    for area, keywords in AREA_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in combined_text:
                impacted.add(area)
                break

    return impacted


def discover_coverage_gaps():
    print("\n🔎 === [TEST COVERAGE GAP ANALYSIS] ===")
    scripts_path = os.path.join(CONFIG_DIR, "scripts.yaml")
    with open(scripts_path, "r", encoding="utf-8") as f:
        scripts = yaml.safe_load(f) or {}

    registry = load_registry()
    registered_tags = set()
    for t in registry.get("tests", []):
        registered_tags.update(t.get("tags", []))
        registered_tags.add(t.get("id"))

    print(f"Total defined scripts in repository: {len(scripts)}")
    covered = []
    uncovered = []

    for s_name in scripts.keys():
        clean_name = s_name.replace("script.", "")
        is_covered = any(clean_name in tag for tag in registered_tags)
        if is_covered:
            covered.append(clean_name)
        else:
            uncovered.append(clean_name)

    print(f"✓ Covered scripts ({len(covered)}): {', '.join(covered[:6])}...")
    print(f"⚠️  Uncovered scripts ({len(uncovered)}):")
    for u in uncovered[:10]:
        print(f"  - {u}")
    if len(uncovered) > 10:
        print(f"  ... and {len(uncovered) - 10} more.")


def generate_scenario_for_script(script_name: str) -> Optional[str]:
    """Self-Building: Synthesizes a new YAML test scenario for an uncovered script."""
    clean_name = script_name.replace("script.", "")
    area = "salon"
    for a, kws in AREA_KEYWORDS.items():
        if any(kw in clean_name for kw in kws):
            area = a
            break

    area_dir = os.path.join(SCENARIOS_DIR, area)
    os.makedirs(area_dir, exist_ok=True)
    scenario_filename = f"test_{clean_name}.yaml"
    scenario_path = os.path.join(area_dir, scenario_filename)

    content = f"""id: test_{clean_name}_execution
area: {area}
description: "Automatically synthesized test scenario for script.{clean_name}"
tags: [{area}, script, generated, regression]
status: active

preconditions:
  entities: {{}}

action:
  type: call_service
  service: script.{clean_name}
  data: {{}}

wait_seconds: 1.0

assertions: []
"""
    with open(scenario_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"✨ Synthesized scenario draft: {scenario_path}")
    return scenario_path


def main():
    parser = argparse.ArgumentParser(description="Autonomous Home Assistant AI Test Runner")
    parser.add_argument("--tier", choices=["0", "1", "2", "3", "all"], default="all", help="Test tier to execute")
    parser.add_argument("--area", help="Filter tests by area/room (e.g. salon, kuchnia, termostaty)")
    parser.add_argument("--auto", action="store_true", help="Auto-detect impacted areas from git diff and test them")
    parser.add_argument("--discover", action="store_true", help="Analyze test coverage gaps")
    parser.add_argument("--generate", help="Synthesize a test scenario for a script or feature name")
    parser.add_argument("--keep-up", action="store_true", help="Keep test containers running after tests")
    parser.add_argument("--stop", action="store_true", help="Stop all test containers")

    args = parser.parse_args()

    if args.stop:
        sandbox.stop_containers()
        print("Test containers stopped.")
        return

    if args.discover:
        discover_coverage_gaps()
        return

    if args.generate:
        path = generate_scenario_for_script(args.generate)
        print(f"Created: {path}")
        return

    target_area = args.area
    if args.auto:
        impacted = detect_git_impacted_areas()
        print(f"🔍 Git Diff Impact Detection: Identified areas -> {impacted or 'None (running all)'}")
        if impacted:
            target_area = list(impacted)[0]  # Focus on primary area

    success = True

    if args.tier in ("0", "all"):
        if not run_tier0():
            sys.exit(1)

    if args.tier in ("1", "all"):
        if not run_tier1():
            sys.exit(1)

    if args.tier in ("2", "all"):
        if not run_tier2(area_filter=target_area):
            success = False

    if args.tier in ("3", "all"):
        if not run_tier3():
            success = False

    if not args.keep_up:
        # Keep running if requested, else leave ready or stop
        pass

    if success:
        print("\n🎉 === ALL TEST TIERS COMPLETED SUCCESSFULLY! ===")
        sys.exit(0)
    else:
        print("\n❌ === SOME TESTS FAILED ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
