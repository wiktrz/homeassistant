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
import re
try:
    import yaml
except ImportError:
    yaml = None
import base64
import hashlib
import hmac

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
SANDBOX_DIR = "/tmp/ha_test_sandbox"
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
OVERLAY_DIR = os.path.join(TESTS_DIR, "test_config_overlay")
DOCKER_COMPOSE_FILE = os.path.join(TESTS_DIR, "docker-compose.test.yml")

# Hermetic stubbing: nothing in the sandbox may reach the real inverter / clouds.
STUB_SCRIPT_NAME = "test_stub_cmd.sh"
STUB_RC_FILE = ".test_stub_rc"  # exit code the stub returns (default 0)
REAL_SCRIPTS = ("pstryk_engine.py", "solarman_v5_client.py", "deye_cloud_client.py")
SANDBOX_SKIP = (".env", ".deye_inverter_config.json", "tesla_fleet.key", ".cloud", "__pycache__")
SECRET_KEY_RE = re.compile(r"(_KEY|_TOKEN|_SECRET|_PASSWORD|_USER|_APP_ID|_APPSECRET|_EMAIL)", re.I)

# Fixed deterministic test JWT credentials
TEST_USER_ID = "ai_test_runner_user_001"
TEST_TOKEN_ID = "ai_test_runner_token_001"
TEST_JWT_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def generate_test_token() -> str:
    """Generates a signed JWT access token accepted by Home Assistant using standard RFC 7519 HS256."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": TEST_TOKEN_ID,
        "iat": now,
        "exp": now + 315360000,  # 10 years
    }
    header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    header_b64 = base64.urlsafe_b64encode(header_json).rstrip(b"=").decode("ascii")
    payload_b64 = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode("ascii")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(
        TEST_JWT_SECRET.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def sanitize_sandbox_secrets(target_dir: str) -> None:
    """
    Sanitizes secrets.yaml in the test sandbox:
    Sensitive string keys (like pstryk_api_key_dol, pstryk_api_key_gora, Camera_URL,
    CameraFront_Source) are replaced with dummy test strings so real secrets never leak into /tmp.
    Numeric values (latitude_home, longitude_home, elevation_home) are strictly preserved.
    """
    secrets_file = os.path.join(target_dir, "secrets.yaml")
    if not os.path.exists(secrets_file):
        return

    preserved_numeric_keys = ("latitude", "longitude", "elevation")
    out_lines = []

    with open(secrets_file, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue

            if ":" in line:
                key_part, val_part = line.split(":", 1)
                k = key_part.strip()
                v = val_part.strip().strip("'\"")

                is_num = False
                try:
                    float(v)
                    is_num = True
                except ValueError:
                    is_num = False

                k_lower = k.lower()
                if is_num and any(p in k_lower for p in preserved_numeric_keys):
                    out_lines.append(line)
                elif "camera" in k_lower and "url" in k_lower:
                    out_lines.append(f'{key_part}: "http://127.0.0.1/dummy_camera"\n')
                elif "camera" in k_lower and "source" in k_lower:
                    out_lines.append(f'{key_part}: "rtsp://127.0.0.1/dummy_stream"\n')
                elif "pstryk_api_key" in k_lower:
                    out_lines.append(f'{key_part}: "sk-dummy-test-key-00000000000000000000000000"\n')
                elif "command" in k_lower:
                    out_lines.append(f'{key_part}: "sh /config/{STUB_SCRIPT_NAME} {k}"\n')
                elif SECRET_KEY_RE.search(k) or any(s in k_lower for s in ("key", "token", "secret", "password", "user", "url", "source")):
                    out_lines.append(f'{key_part}: "dummy_test_{k}"\n')
                elif is_num:
                    out_lines.append(line)
                else:
                    out_lines.append(f'{key_part}: "dummy_{k}"\n')
            else:
                out_lines.append(line)

    with open(secrets_file, "w", encoding="utf-8") as f:
        f.writelines(out_lines)


def write_sandbox_env(target_dir: str):
    """Writes a sandbox .env from .env.example: loopback inverter, all secrets blank."""
    out = []
    with open(os.path.join(CONFIG_DIR, ".env.example"), encoding="utf-8") as f:
        for line in f:
            key = line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#"):
                if key == "DEYE_INVERTER_IP":
                    line = f"{key}=127.0.0.1\n"
                elif SECRET_KEY_RE.search(key) or key.startswith("DEYE_CLOUD_"):
                    line = f"{key}=\n"
            out.append(line)
    with open(os.path.join(target_dir, ".env"), "w", encoding="utf-8") as f:
        f.writelines(out)


def stub_commands(text: str) -> str:
    """Text rewrite (keeps !include/!secret intact): every shell_command entry and
    every command_line command calling a real engine/client script becomes the stub."""
    stub = f"sh /config/{STUB_SCRIPT_NAME}"
    lines = text.splitlines(keepends=True)
    out, section, i = [], None, 0

    def indent(l):
        return len(l) - len(l.lstrip())

    def take_value(start, key_indent):
        j = start + 1
        while j < len(lines) and (not lines[j].strip() or indent(lines[j]) > key_indent):
            j += 1
        return "".join(lines[start:j]), j

    while i < len(lines):
        line = lines[i]
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            section = line.split(":", 1)[0].strip()
        m_shell = re.match(r"^(\s+)([A-Za-z0-9_]+):", line) if section == "shell_command" else None
        m_cmd = re.match(r"^(\s+)command:", line) if section == "command_line" else None
        if m_shell and indent(line) == 2:
            _, i = take_value(i, 2)
            out.append(f'  {m_shell.group(2)}: "{stub} {m_shell.group(2)}"\n')
            continue
        if m_cmd:
            value, j = take_value(i, len(m_cmd.group(1)))
            if any(s in value for s in REAL_SCRIPTS):
                out.append(f'{m_cmd.group(1)}command: "{stub} command_line"\n')
                i = j
                continue
        out.append(line)
        i += 1
    return "".join(out)


def create_sandbox(target_dir: str = SANDBOX_DIR) -> str:
    """Creates an isolated, hermetic test sandbox (default /tmp/ha_test_sandbox)."""
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)

    os.makedirs(target_dir, exist_ok=True)

    # Sync config excluding databases, locks, logs, real secrets and inverter cache
    ignore_patterns = shutil.ignore_patterns(
        "*.db", "*.db-shm", "*.db-wal", ".ha_run.lock", "*.log", "*.log.*", *SANDBOX_SKIP
    )
    shutil.copytree(CONFIG_DIR, target_dir, dirs_exist_ok=True, ignore=ignore_patterns)
    write_sandbox_env(target_dir)
    sanitize_sandbox_secrets(target_dir)
    shutil.copy(os.path.join(OVERLAY_DIR, STUB_SCRIPT_NAME), os.path.join(target_dir, STUB_SCRIPT_NAME))

    # Ensure .storage directory exists
    storage_dir = os.path.join(target_dir, ".storage")
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

    # Drop any real MQTT entry so the sandbox can only talk to ha-test-mqtt
    entries = [e for e in entries_data.setdefault("data", {}).get("entries", []) if e.get("domain") != "mqtt"]
    entries_data["data"]["entries"] = entries
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
    config_yaml_file = os.path.join(target_dir, "configuration.yaml")
    with open(config_yaml_file, "r", encoding="utf-8") as f:
        config_text = stub_commands(f.read())

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
    with open(config_yaml_file, "w", encoding="utf-8") as f:
        f.write(config_text + test_append)

    return target_dir


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
    """Polls Home Assistant API until it responds healthy and state is RUNNING."""
    import urllib.request
    import urllib.error

    url = f"http://127.0.0.1:{port}/api/config"
    token = generate_test_token()
    headers = {"Authorization": f"Bearer {token}"}

    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    if data.get("state") == "RUNNING":
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
