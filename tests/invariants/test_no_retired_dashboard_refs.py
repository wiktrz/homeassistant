"""
INVARIANT: the retired dashboard file must not be referenced anywhere in the
config, tests or scripts trees. The needle is assembled at runtime so this
file does not match itself.
"""

import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCAN_DIRS = ["config", "tests", "scripts"]
NEEDLE = "dashboard_home" + "_improved"


def test_retired_dashboard_not_referenced():
    hits = []
    for scan_dir in SCAN_DIRS:
        root_dir = os.path.join(REPO_ROOT, scan_dir)
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                fpath = os.path.join(dirpath, name)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for line_no, line in enumerate(f, 1):
                            if NEEDLE in line:
                                hits.append(f"{os.path.relpath(fpath, REPO_ROOT)}:{line_no}")
                except OSError:
                    continue
    assert not hits, f"Retired dashboard referenced: {hits}"
