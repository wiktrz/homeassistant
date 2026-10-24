"""SPEC must keep the authoritative TOU requirements section with IDs R1..R7."""
import re
from pathlib import Path

SPEC = (Path(__file__).resolve().parents[2] / "SPEC_DEYE_BATTERY_SYSTEM.md").read_text()


def test_heading_present():
    assert "TOU planning requirements (authoritative)" in SPEC


def test_requirement_ids_present():
    for i in range(1, 8):
        assert re.search(rf"\*\*R{i} ", SPEC), f"R{i} missing"
