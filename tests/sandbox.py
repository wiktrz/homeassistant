#!/usr/bin/env python3
"""
Sandbox Manager for Ephemeral Home Assistant Testing.
Creates an isolated copy-on-write workspace in /tmp/ha_test_sandbox,
pre-seeds authentication & onboarding, mounts in-memory SQLite,
and manages the lifecycle of the test Docker containers.
"""

import os
import shutil
import subprocess
import time
import json
import uuid
import jwt
import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
SANDBOX_DIR = "/tmp/ha_test_sandbox"
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
OVERLAY_DIR = os.path.join(TESTS_DIR, "test_config_overlay")
DOCKER_COMPOSE_FILE = os.path.join(TESTS_DIR, "docker-compose.test.yml")

# Fixed deterministic test JWT credentials
TEST_USER_ID = "ai_test_runner_user_001"
TEST_TOKEN_ID = "ai_test_runner_token_001"
TEST_JWT_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def generate_test_token() -> str:
    """Generates a signed JWT access token accepted by Home Assistant."""
    now = int(time.time())
    payload = {
        "iss": TEST_TOKEN_ID,
        "iat": now,
        "exp": now + 315360000,  # 10 years
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


def create_sandbox() -> str:
    """Creates an isolated test sandbox in /tmp/ha_test_sandbox."""
    if os.path.exists(SANDBOX_DIR):
        shutil.rmtree(SANDBOX_DIR)

    os.makedirs(SANDBOX_DIR, exist_ok=True)

    # Sync config excluding databases, locks, and logs
    ignore_patterns = shutil.ignore_patterns(
        "*.db", "*.db-shm", "*.db-wal", ".ha_run.lock", "*.log", "*.log.*"
    )
    shutil.copytree(CONFIG_DIR, SANDBOX_DIR, dirs_exist_ok=True, ignore=ignore_patterns)

    # Ensure .storage directory exists
    storage_dir = os.path.join(SANDBOX_DIR, ".storage")
    os.makedirs(storage_dir, exist_ok=True)

    # 1. Inject onboarding state (mark all done)
    onboarding_src = os.path.join(OVERLAY_DIR, "onboarding.json")
    if os.path.exists(onboarding_src):
        shutil.copy(onboarding_src, os.path.join(storage_dir, "onboarding"))

    # 2. Inject Auth & Long Lived Access Token
    auth_file = os.path.join(storage_dir, "auth")
    if os.path.exists(auth_file):
        try:
            with open(auth_file, "r", encoding="utf-8") as f:
                auth_data = json.load(f)
        except Exception:
            auth_data = {"version": 1, "minor_version": 4, "key": "auth", "data": {}}
    else:
        auth_data = {"version": 1, "minor_version": 4, "key": "auth", "data": {}}

    users = auth_data.setdefault("data", {}).setdefault("users", [])
    user_id = TEST_USER_ID
    if users:
        for u in users:
            u["is_owner"] = True
            u["is_active"] = True
            u["group_ids"] = ["system-admin"]
        user_id = users[0].get("id", TEST_USER_ID)
    else:
        users.append({
            "id": user_id,
            "username": "ai_test_runner",
            "name": "AI Test Runner",
            "is_owner": True,
            "is_active": True,
            "system_generated": False,
            "group_ids": ["system-admin"],
        })

    # Add refresh token for the test token
    refresh_tokens = auth_data["data"].setdefault("refresh_tokens", [])
    # Remove previous test token if present
    refresh_tokens = [rt for rt in refresh_tokens if rt.get("id") != TEST_TOKEN_ID]
    refresh_tokens.append({
        "id": TEST_TOKEN_ID,
        "user_id": user_id,
        "client_id": None,
        "client_name": "AI Test Runner",
        "client_icon": None,
        "token_type": "long_lived_access_token",
        "created_at": "2026-09-23T20:00:00.000000+00:00",
        "access_token_expiration": 315360000.0,
        "token": "",
        "jwt_key": TEST_JWT_SECRET,
        "last_used_at": None,
        "last_used_ip": None,
        "expire_at": None,
        "credential_id": None,
        "version": "2026.2.3",
    })
    auth_data["data"]["refresh_tokens"] = refresh_tokens

    with open(auth_file, "w", encoding="utf-8") as f:
        json.dump(auth_data, f, indent=2)

    # 3. Inject MQTT config entry into core.config_entries
    config_entries_file = os.path.join(storage_dir, "core.config_entries")
    if os.path.exists(config_entries_file):
        try:
            with open(config_entries_file, "r", encoding="utf-8") as f:
                entries_data = json.load(f)
        except Exception:
            entries_data = {"version": 1, "minor_version": 5, "key": "core.config_entries", "data": {"entries": []}}
    else:
        entries_data = {"version": 1, "minor_version": 5, "key": "core.config_entries", "data": {"entries": []}}

    entries = entries_data.setdefault("data", {}).setdefault("entries", [])
    has_mqtt = any(e.get("domain") == "mqtt" for e in entries)
    if not has_mqtt:
        entries.append({
            "created_at": "2026-03-01T19:15:08.000000+00:00",
            "data": {
                "broker": "ha-test-mqtt",
                "port": 1883,
                "discovery": False,
            },
            "disabled_by": None,
            "discovery_keys": {},
            "domain": "mqtt",
            "entry_id": "01KJND6VMQTTTESTCONFIGENTRY01",
            "minor_version": 1,
            "modified_at": "2026-03-01T19:15:08.000000+00:00",
            "options": {},
            "pref_disable_new_entities": False,
            "pref_disable_polling": False,
            "source": "user",
            "subentries": [],
            "title": "MQTT Test Broker",
            "unique_id": None,
            "version": 1,
        })
    with open(config_entries_file, "w", encoding="utf-8") as f:
        json.dump(entries_data, f, indent=2)

    # 4. Modify configuration.yaml in sandbox to optimize for testing
    config_yaml_file = os.path.join(SANDBOX_DIR, "configuration.yaml")
    with open(config_yaml_file, "r", encoding="utf-8") as f:
        config_text = f.read()

    # Append test optimizations: in-memory sqlite recorder, trusted networks
    test_append = """
# === TEST SANDBOX OVERRIDES ===
recorder:
  db_url: 'sqlite:////tmp/ha_test_recorder.db'
  commit_interval: 1

http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - 172.16.0.0/12
    - 192.168.0.0/16
    - 10.0.0.0/8

lovelace:
  mode: storage
  dashboards:
    dashboard-home:
      mode: yaml
      title: Dom iPad Modern
      icon: mdi:home
      show_in_sidebar: true
      filename: dashboard_modern_reference.yaml
    dashboard-improved:
      mode: yaml
      title: Dom Improved
      icon: mdi:home-analytics
      show_in_sidebar: true
      filename: dashboard_improved.yaml

"""
    with open(config_yaml_file, "a", encoding="utf-8") as f:
        f.write(test_append)

    return SANDBOX_DIR


def start_containers() -> bool:
    """Spins up ha-test-mqtt and ha-test via docker compose."""
    cmd = ["docker", "compose", "-f", DOCKER_COMPOSE_FILE, "up", "-d"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Failed to start test containers: {res.stderr}")
        return False
    return True


def stop_containers(remove_volumes: bool = True):
    """Tears down test containers."""
    cmd = ["docker", "compose", "-f", DOCKER_COMPOSE_FILE, "down"]
    if remove_volumes:
        cmd.append("-v")
    subprocess.run(cmd, capture_output=True, text=True)


def wait_for_ha_ready(timeout_seconds: int = 40, port: int = 8125) -> bool:
    """Polls Home Assistant API until it responds healthy."""
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{port}/api/"
    token = generate_test_token()
    headers = {"Authorization": f"Bearer {token}"}

    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    if data.get("message") == "API running.":
                        return True
        except Exception:
            pass
        time.sleep(1)

    return False


if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "create"
    if action == "create":
        print(f"Sandbox created at: {create_sandbox()}")
        print(f"Test Token: {generate_test_token()}")
    elif action == "start":
        create_sandbox()
        start_containers()
        print("Waiting for HA...")
        ready = wait_for_ha_ready()
        print(f"HA Ready: {ready}")
    elif action == "stop":
        stop_containers()
        print("Containers stopped.")
