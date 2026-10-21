#!/usr/bin/env python3
"""
Tier 1A invariant: the Tier 2 test sandbox must be hermetic.

The ephemeral HA test instance must never reach the real Deye inverter, the
Deye Cloud or Pstryk APIs. Builds the sandbox into a tmp dir (no Docker) and
checks the .env and every shell_command / command_line command.
"""

import ipaddress
import os
import re
import sys

import pytest
import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
import sandbox  # noqa: E402

REAL_SCRIPTS = ("pstryk_engine.py", "solarman_v5_client.py", "deye_cloud_client.py")
SECRET_KEY_RE = re.compile(r"(_KEY|_TOKEN|_SECRET|_PASSWORD|_USER|_APP_ID|_APPSECRET|_EMAIL)", re.I)
IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that accepts HA tags (!include, !secret, ...) as plain strings."""


_TagTolerantLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: f"!{suffix} {getattr(node, 'value', '')}"
)


def _no_duplicate_keys(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"Duplicate YAML keys in sandbox config: {dupes}"
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_TagTolerantLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


@pytest.fixture(scope="module")
def sandbox_dir(tmp_path_factory):
    return sandbox.create_sandbox(str(tmp_path_factory.mktemp("ha_sandbox")))


@pytest.fixture(scope="module")
def sandbox_config(sandbox_dir):
    with open(os.path.join(sandbox_dir, "configuration.yaml"), encoding="utf-8") as f:
        return yaml.load(f, Loader=_TagTolerantLoader)


def _parse_env(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def _all_commands(cfg):
    cmds = [(f"shell_command.{k}", str(v)) for k, v in (cfg.get("shell_command") or {}).items()]
    for item in cfg.get("command_line") or []:
        for platform, body in item.items():
            if isinstance(body, dict) and "command" in body:
                cmds.append((f"command_line.{platform}.{body.get('name')}", str(body["command"])))
    return cmds


def test_sandbox_env_is_blanked(sandbox_dir):
    env_path = os.path.join(sandbox_dir, ".env")
    if not os.path.exists(env_path):
        return
    env = _parse_env(env_path)
    ip = env.get("DEYE_INVERTER_IP", "")
    assert ip and ipaddress.ip_address(ip).is_loopback, "Sandbox DEYE_INVERTER_IP must be loopback"
    for key, value in env.items():
        if SECRET_KEY_RE.search(key) or key.startswith("DEYE_CLOUD_"):
            assert value == "", f"Sandbox .env key {key} must be blank"


def test_sandbox_has_no_real_inverter_cache(sandbox_dir):
    assert not os.path.exists(os.path.join(sandbox_dir, ".deye_inverter_config.json"))


def test_sandbox_config_loads_with_ha_tags(sandbox_config):
    assert sandbox_config["automation"].startswith("!include")
    assert sandbox_config["shell_command"], "shell_command section vanished"


def test_no_command_reaches_real_hardware(sandbox_config):
    commands = _all_commands(sandbox_config)
    assert commands, "No commands found; parser broken?"
    for name, cmd in commands:
        for script in REAL_SCRIPTS:
            assert script not in cmd, f"{name} still calls {script}"
        for ip in IPV4_RE.findall(cmd):
            assert ipaddress.ip_address(ip).is_loopback, f"{name} references real host {ip}"


def test_every_shell_command_is_stubbed(sandbox_config):
    for name, cmd in (sandbox_config.get("shell_command") or {}).items():
        assert sandbox.STUB_SCRIPT_NAME in cmd, f"shell_command.{name} is not stubbed"


def test_stub_script_present(sandbox_dir):
    assert os.path.exists(os.path.join(sandbox_dir, sandbox.STUB_SCRIPT_NAME))


def test_mqtt_points_at_test_broker(sandbox_dir):
    import json

    with open(os.path.join(sandbox_dir, ".storage", "core.config_entries"), encoding="utf-8") as f:
        entries = json.load(f)["data"]["entries"]
    brokers = [e["data"].get("broker") for e in entries if e.get("domain") == "mqtt"]
    assert brokers == ["ha-test-mqtt"], f"Sandbox MQTT must only use ha-test-mqtt, got {brokers}"
