#!/usr/bin/env python3
"""
Pre-Check Hook & Variable Reuse Validator.
Enforces Rule 10 from AI_DEVELOPMENT_GUIDELINES.md.

Usage:
  # 1. Search existing variables before adding a new one:
  python3 scripts/check_duplicate_variable.py --search "reserve"
  python3 scripts/check_duplicate_variable.py --search "capacity"
  python3 scripts/check_duplicate_variable.py --search "current"

  # 2. Validate that no code files duplicate fallback constants:
  python3 scripts/check_duplicate_variable.py --validate
"""

import argparse
import ast
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")


def search_existing_definitions(term: str) -> None:
    term_lower = term.lower().strip()
    print(f"\n🔍 Searching for existing variables/helpers matching: '{term}'\n")

    matches_found = 0

    # 1. Search .env.example
    env_example = os.path.join(CONFIG_DIR, ".env.example")
    if os.path.exists(env_example):
        with open(env_example, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean = line.strip()
                if not clean or clean.startswith("#"):
                    continue
                if term_lower in clean.lower():
                    matches_found += 1
                    print(f"  [.env.example:{line_no}] {clean}")

    # 2. Search config/energy_defaults.py constants
    ed_path = os.path.join(CONFIG_DIR, "energy_defaults.py")
    if os.path.exists(ed_path):
        with open(ed_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean = line.strip()
                if clean.startswith("#") or "=" not in clean:
                    continue
                var_name = clean.split("=", 1)[0].strip()
                if var_name.isupper() and term_lower in var_name.lower():
                    matches_found += 1
                    print(f"  [energy_defaults.py:{line_no}] {var_name}")

    # 3. Search configuration.yaml helpers
    cfg_path = os.path.join(CONFIG_DIR, "configuration.yaml")
    if os.path.exists(cfg_path):
        helper_pattern = re.compile(r"^\s*([a-z0-9_]+):\s*$")
        domain = None
        with open(cfg_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                raw = line.rstrip()
                if re.match(r"^input_[a-z]+:", raw):
                    domain = raw.split(":", 1)[0].strip()
                    continue
                elif re.match(r"^[a-z_]+:", raw) and not raw.startswith(" "):
                    domain = None
                if domain and raw.startswith("  ") and not raw.startswith("    "):
                    m = helper_pattern.match(raw)
                    if m:
                        helper_name = f"{domain}.{m.group(1)}"
                        if term_lower in helper_name.lower():
                            matches_found += 1
                            print(f"  [configuration.yaml:{line_no}] {helper_name}")

    # 4. Search templates.yaml sensors
    tpl_path = os.path.join(CONFIG_DIR, "templates.yaml")
    if os.path.exists(tpl_path):
        with open(tpl_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean = line.strip()
                if clean.startswith("name:") or clean.startswith("unique_id:"):
                    if term_lower in clean.lower():
                        matches_found += 1
                        print(f"  [templates.yaml:{line_no}] {clean}")

    if matches_found == 0:
        print(f"  (No existing definitions found for '{term}'.)")
    else:
        print(f"\n💡 Found {matches_found} existing candidate(s). REUSE existing definitions where possible!\n")


def safe_ast_unparse(node: ast.AST) -> str:
    """Safe ast.unparse helper with Python 3.8 fallback."""
    if hasattr(ast, "unparse"):
        return ast.unparse(node)
    if isinstance(node, ast.Call):
        func_name = getattr(node.func, "attr", getattr(node.func, "id", "call"))
        args_str = ", ".join(
            repr(getattr(a, "value", str(a))) if isinstance(a, ast.Constant) else type(a).__name__
            for a in node.args
        )
        return f"{func_name}({args_str})"
    return ast.dump(node)


def find_get_literal_fallbacks(fpath: str) -> List[str]:
    """Flag `.get(<key>, <non-zero int/float literal>)`: fallbacks must come from energy_defaults."""
    rel_path = os.path.relpath(fpath, REPO_ROOT)
    with open(fpath, "r", encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=fpath)
    except Exception:
        return []  # parse errors are reported by the AST check in validate_no_duplicate_defaults
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and len(n.args) == 2 and isinstance(n.args[1], ast.Constant)
                and isinstance(n.args[1].value, (int, float)) and not isinstance(n.args[1].value, bool)
                and n.args[1].value != 0):
            out.append(f"{rel_path}:{n.lineno} [GET FALLBACK LITERAL: {n.args[1].value}]: {safe_ast_unparse(n)}")
    return out


# Alias for backward and forward compatibility
check_inline_get_fallbacks = find_get_literal_fallbacks


def validate_no_duplicate_defaults() -> List[str]:
    violations: List[str] = []

    files_to_check = [
        os.path.join(CONFIG_DIR, "pstryk_engine.py"),
        os.path.join(CONFIG_DIR, "solarman_v5_client.py"),
        os.path.join(CONFIG_DIR, "deye_cloud_client.py"),
    ]

    # Forbidden literal numbers in fallback / parameter default context
    forbidden_literals = {
        "16.13": "Battery capacity literal 16.13 kWh",
        "51.2": "Nominal voltage literal 51.2 V",
        "100.0": "Max charge current literal 100.0 A",
    }

    for fpath in files_to_check:
        if not os.path.exists(fpath):
            continue
        violations.extend(find_get_literal_fallbacks(fpath))
        rel_path = os.path.relpath(fpath, REPO_ROOT)

        # 1. Text check for raw forbidden literals in assignments or fallbacks
        with open(fpath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''") or stripped.startswith("*"):
                continue
            for lit, desc in forbidden_literals.items():
                if lit in stripped:
                    # Allow docstrings or comments that might be inside a line
                    # But flag code assignments like: `else 16.13`, `= 16.13`, `51.2`
                    if re.search(r"(?:else|=|\breturn\b|\bor\b)\s*['\"]?" + re.escape(lit), stripped):
                        violations.append(f"{rel_path}:{line_no} [RAW LITERAL: {desc}]: {stripped}")

        # 2. AST check for function parameter defaults
        try:
            tree = ast.parse("".join(lines), filename=fpath)
        except Exception as e:
            violations.append(f"{rel_path}: AST parse error: {e}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip private helper functions if any, but check all public / dispatch functions
                for default in node.args.defaults:
                    if isinstance(default, ast.Constant):
                        val = default.value
                        # Check numeric defaults matching dispatch constants
                        if isinstance(val, (int, float)) and val in (16.13, 51.2, 100.0):
                            violations.append(
                                f"{rel_path}:{node.lineno} [PARAM DEFAULT: {val} in '{node.name}'] must default to None and resolve via energy_defaults"
                            )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-Check Hook & Variable Reuse Validator")
    parser.add_argument("--search", type=str, help="Search existing variables/helpers before creating a new one")
    parser.add_argument("--validate", action="store_true", help="Validate that no code files duplicate fallback constants")

    args = parser.parse_args()

    if args.search:
        search_existing_definitions(args.search)
        return 0

    if args.validate:
        violations = validate_no_duplicate_defaults()
        if violations:
            print("\n❌ VIOLATIONS DETECTED: Hardcoded fallback constants found in code:")
            for v in violations:
                print(f"  - {v}")
            print("\nAll fallbacks must reside strictly in .env.example / .env and be imported via config/energy_defaults.py!\n")
            return 1
        else:
            print("✅ Pre-check validation passed: zero hardcoded fallback literals in Python code.")
            return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
