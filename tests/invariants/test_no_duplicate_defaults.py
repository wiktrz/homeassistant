#!/usr/bin/env python3
"""
Invariant Test: Enforce Single Source of Truth for Energy Defaults.
Enforces Rule 10 from AI_DEVELOPMENT_GUIDELINES.md.
Guarantees that:
1. No raw fallback literals (16.13, 51.2, 100.0) exist in Python code.
2. All modules import and resolve fallbacks via config.energy_defaults.
"""

import os
import subprocess
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_no_hardcoded_defaults_in_python_code():
    """
    Runs scripts/check_duplicate_variable.py --validate to ensure
    zero raw fallback constants exist in Python codebase.
    """
    validator_script = os.path.join(REPO_ROOT, "scripts", "check_duplicate_variable.py")
    assert os.path.exists(validator_script), f"Missing {validator_script}"

    # Import the validator directly
    import sys
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import check_duplicate_variable as cdv

    violations = cdv.validate_no_duplicate_defaults()
    assert not violations, (
        "HARDCODED DEFAULTS VIOLATION (Rule 10):\n"
        "Found hardcoded fallback literals in Python code:\n" + "\n".join(f"  - {v}" for v in violations) +
        "\nAll fallback defaults must reside strictly in .env.example / .env and be imported via energy_defaults.py!"
    )


def test_validator_flags_get_numeric_fallback(tmp_path):
    """The Rule 10 AST rule flags .get(key, <non-zero literal>) and allows 0 / named constants."""
    import sys
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import check_duplicate_variable as cdv

    bad = tmp_path / "bad.py"
    bad.write_text("x = d.get('x', 5000)\ny = d.get('y', 0)\nz = d.get('z', ed.NAME)\n")
    found = cdv.find_get_literal_fallbacks(str(bad))
    assert len(found) == 1 and "5000" in found[0]


def test_energy_defaults_module_loads_correctly():
    """Verifies that config/energy_defaults.py resolves all required constants from .env.example."""
    import sys
    config_dir = os.path.join(REPO_ROOT, "config")
    if config_dir not in sys.path:
        sys.path.insert(0, config_dir)

    import energy_defaults as ed

    # Hardware fallbacks (read from .env.example template)
    assert ed.FALLBACK_BATTERY_CAPACITY_KWH == 16.13
    assert ed.FALLBACK_BATTERY_NOMINAL_VOLTAGE == 51.2
    assert ed.FALLBACK_BATTERY_MAX_CHARGE_CURRENT == 100.0
    assert ed.FALLBACK_BATTERY_CAPACITY_AH == 315.0

    # Inverter power limits
    assert ed.INVERTER_MAX_POWER_W == 12000
    assert ed.INVERTER_MIN_POWER_W == 1000
    assert ed.DEFAULT_TRANSFER_POWER_W == 5000

    # SOC bounds and defaults
    assert ed.DEFAULT_MAX_SOC == 90
    assert ed.DEFAULT_MIN_SOC == 20
    assert ed.DEFAULT_SHUTDOWN_SOC == 5
    assert ed.DEFAULT_CURRENT_SOC == 85
    assert ed.DEFAULT_MORNING_RESERVE_MARGIN == 10
    assert ed.DEFAULT_PRE_PEAK_MARGIN == 20
    assert ed.DEFAULT_LOSS_BUFFER_PCT == 10.0


def test_alias_synchronization():
    """Verifies that BATTERY_CHARGE_CURRENT_A and BATTERY_MAX_CHARGE_CURRENT are interchangeable."""
    import sys
    config_dir = os.path.join(REPO_ROOT, "config")
    if config_dir not in sys.path:
        sys.path.insert(0, config_dir)

    import energy_defaults as ed

    # Alias check
    alias_val = ed.get_default_float("BATTERY_MAX_CHARGE_CURRENT", alias="BATTERY_CHARGE_CURRENT_A")
    assert alias_val == 100.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
