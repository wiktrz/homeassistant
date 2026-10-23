"""Exactly one .env.example (config/.env.example); no code path reads a root-level .env/.env.example."""
import os
import re
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCAN = ("config", "scripts", "tests", ".claude/hooks", ".codex/hooks")
# root-style references: os.path.join(<root var>, ".env...") or REPO_ROOT/_REPO_ROOT variants
ROOT_REF = re.compile(r'join\(\s*_?(?:REPO_ROOT|repo_root|ROOT)\s*,\s*"\.env')


def test_exactly_one_env_example():
    found = []
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in (".git", "node_modules", "__pycache__")]
        found += [os.path.relpath(os.path.join(d, f), ROOT) for f in files if f == ".env.example"]
    assert found == [os.path.join("config", ".env.example")], found
    assert not os.path.exists(os.path.join(ROOT, ".env"))


def test_no_root_env_references():
    bad = []
    for top in SCAN:
        for d, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for f in files:
                if not f.endswith(".py") or f == os.path.basename(__file__):
                    continue
                p = os.path.join(d, f)
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    for n, line in enumerate(fh, 1):
                        if ROOT_REF.search(line):
                            bad.append(f"{os.path.relpath(p, ROOT)}:{n}")
    assert not bad, bad
