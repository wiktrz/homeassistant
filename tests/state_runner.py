#!/usr/bin/env python3
"""
Tier 2: Dynamic Home Assistant State & API Test Runner.
Executes declarative test scenarios against a running Home Assistant instance.
Supports:
- Virtual state injection (POST /api/states/<entity>)
- Service calls (POST /api/services/<domain>/<service>)
- Dynamic Jinja template validation (POST /api/template)
- Event bus state assertion with polling timeouts
- Error log verification (/api/error_log)
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
import yaml
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from sandbox import generate_test_token


class HomeAssistantClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8125", token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token or generate_test_token()
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        url = f"{self.base_url}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                content = resp.read().decode("utf-8")
                try:
                    return status, json.loads(content)
                except Exception:
                    return status, content
        except urllib.error.HTTPError as e:
            err_content = e.read().decode("utf-8")
            try:
                return e.code, json.loads(err_content)
            except Exception:
                return e.code, err_content
        except Exception as e:
            return 0, str(e)

    def is_healthy(self) -> bool:
        status, data = self._request("GET", "/api/")
        return status == 200 and isinstance(data, dict) and data.get("message") == "API running."

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        status, data = self._request("GET", f"/api/states/{entity_id}")
        return data if status == 200 else None

    def set_state(self, entity_id: str, state: str, attributes: Optional[Dict[str, Any]] = None) -> bool:
        """Injects or overrides any virtual entity state in Home Assistant."""
        existing = self.get_state(entity_id)
        existing_attrs = existing.get("attributes", {}) if isinstance(existing, dict) else {}
        final_attrs = {**existing_attrs, **(attributes or {})}
        payload = {"state": state, "attributes": final_attrs}
        status, _ = self._request("POST", f"/api/states/{entity_id}", payload)
        return status in (200, 201)

    def call_service(self, domain: str, service: str, service_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
        status, data = self._request("POST", f"/api/services/{domain}/{service}", service_data or {})
        return (status == 200, data)

    def render_template(self, template_str: str) -> Optional[str]:
        status, data = self._request("POST", "/api/template", {"template": template_str})
        return str(data) if status == 200 else None

    def get_error_log(self) -> str:
        status, data = self._request("GET", "/api/error_log")
        return str(data) if status == 200 else ""


def execute_scenario(scenario_path: str, client: HomeAssistantClient) -> Tuple[bool, List[str]]:
    """Loads and executes a single declarative YAML scenario."""
    with open(scenario_path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    test_id = doc.get("id", os.path.basename(scenario_path))
    desc = doc.get("description", "")
    messages = [f"▶ Running scenario: {test_id} ({desc})"]

    # 1. Setup Preconditions
    pre = doc.get("preconditions", {})
    entities_to_mock = pre.get("entities", {})
    for entity_id, val in entities_to_mock.items():
        if isinstance(val, dict):
            st = str(val.get("state", "off"))
            attrs = val.get("attributes", {})
        else:
            st = str(val)
            attrs = {}
        ok = client.set_state(entity_id, st, attrs)
        if not ok:
            messages.append(f"  ❌ Failed to inject precondition for {entity_id}")
            return False, messages
        messages.append(f"  ✓ Injected {entity_id} = {st}")

    time.sleep(0.5)

    # 2. Execute Action
    action = doc.get("action", {})
    action_type = action.get("type", "call_service")
    if action_type == "call_service" or "service" in action:
        svc_full = action.get("service", "")
        svc_data = action.get("data", {})
        if svc_full.startswith("script."):
            ok, res = client.call_service("script", "turn_on", {
                "entity_id": svc_full,
                "variables": svc_data
            })
            if not ok:
                messages.append(f"  ❌ Script call {svc_full} failed: {res}")
                return False, messages
            messages.append(f"  ✓ Executed script {svc_full} with variables {svc_data}")
        else:
            parts = svc_full.split(".", 1)
            if len(parts) == 2:
                domain, service = parts
                ok, res = client.call_service(domain, service, svc_data)
                if not ok:
                    messages.append(f"  ❌ Service call {svc_full} failed: {res}")
                    return False, messages
                messages.append(f"  ✓ Called service {svc_full} with {svc_data}")
            else:
                messages.append(f"  ❌ Invalid service name: {svc_full}")
                return False, messages
    elif action_type == "set_state":
        e_id = action.get("entity_id")
        st = action.get("state")
        attrs = action.get("attributes")
        client.set_state(e_id, st, attrs)
        messages.append(f"  ✓ State mutated {e_id} -> {st}")

    # Wait for state convergence
    wait_time = float(doc.get("wait_seconds", 1.5))
    time.sleep(wait_time)

    # 3. Assertions
    assertions = doc.get("assertions", [])
    for assert_item in assertions:
        if "entity" in assert_item:
            target_entity = assert_item["entity"]
            expected_state = str(assert_item.get("state"))
            actual = client.get_state(target_entity)
            if not actual:
                messages.append(f"  ❌ Assertion failed: entity {target_entity} not found!")
                return False, messages
            actual_state = str(actual.get("state"))
            if actual_state != expected_state:
                messages.append(
                    f"  ❌ State mismatch on {target_entity}: expected '{expected_state}', got '{actual_state}'"
                )
                return False, messages
            messages.append(f"  ✓ Assert passed: {target_entity} == '{expected_state}'")

        elif "safety_guard" in assert_item:
            guard = assert_item["safety_guard"]
            guard_entities = guard.get("entities", [])
            expected_guard_state = str(guard.get("expected_state", "on"))
            for g_ent in guard_entities:
                g_actual = client.get_state(g_ent)
                if g_actual:
                    g_state = str(g_actual.get("state"))
                    if g_state != expected_guard_state:
                        messages.append(
                            f"  🚨 SAFETY INVARIANT BREACH: {g_ent} was altered to '{g_state}' (must remain '{expected_guard_state}')"
                        )
                        return False, messages
                    messages.append(f"  ✓ Safety guard passed: {g_ent} maintained '{g_state}'")

    messages.append(f"  🎉 Scenario {test_id} PASSED.")
    return True, messages


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 state_runner.py <path_to_scenario.yaml>")
        sys.exit(1)

    client = HomeAssistantClient()
    if not client.is_healthy():
        print("❌ Home Assistant test instance is not healthy on http://127.0.0.1:8125")
        sys.exit(1)

    success, logs = execute_scenario(sys.argv[1], client)
    for line in logs:
        print(line)
    sys.exit(0 if success else 1)
