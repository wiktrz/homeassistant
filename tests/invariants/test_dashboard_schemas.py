#!/usr/bin/env python3
"""
Invariant Test: Enforce Lovelace Dashboard Schema Integrity & Structural Indentation.
Enforces Rule 11 from AI_DEVELOPMENT_GUIDELINES.md.

Guarantees that:
1. Every YAML dashboard in config/dashboards/*.yaml parses valid YAML syntax.
2. Every card with type: entities (standalone or nested) has a non-empty entities: list.
3. Every card with type: conditional has a valid card: mapping and conditions: list,
   and NEVER has entities: or cards: as siblings of card: (preventing 8 vs 10 space indentation bugs).
4. Every conditional row inside an entities card has a valid row: mapping and conditions: list.
5. Every container card (vertical-stack, horizontal-stack, grid) has a non-empty cards: list.
"""

import os
import glob
import pytest
import yaml
from typing import List, Tuple, Dict, Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DASHBOARDS_DIR = os.path.join(REPO_ROOT, "config", "dashboards")


def find_cards_and_rows(node: Any, path: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    """
    Recursively traverses arbitrary YAML structures to find all card and row mappings
    containing a 'type' attribute.
    """
    items = []
    if isinstance(node, dict):
        if "type" in node:
            items.append((path, node))
        for k, v in node.items():
            child_path = f"{path}.{k}" if path else str(k)
            items.extend(find_cards_and_rows(v, child_path))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            child_path = f"{path}[{idx}]"
            items.extend(find_cards_and_rows(item, child_path))
    return items


def validate_dashboard_cards(dashboard_path: str, data: Any) -> List[str]:
    """
    Validates all cards and rows in a parsed dashboard structure.
    Returns a list of error violation strings.
    """
    violations = []
    items = find_cards_and_rows(data)

    for path, element in items:
        element_type = element.get("type")

        # 1. Validate entities cards
        if element_type == "entities":
            if "entities" not in element:
                violations.append(
                    f"{dashboard_path} at '{path}': card 'type: entities' is missing mandatory 'entities:' list."
                )
            elif not isinstance(element["entities"], list):
                violations.append(
                    f"{dashboard_path} at '{path}': 'entities:' must be a list, got {type(element['entities']).__name__}."
                )
            elif len(element["entities"]) == 0:
                violations.append(
                    f"{dashboard_path} at '{path}': 'entities:' list cannot be empty."
                )

        # 2. Validate conditional elements (cards vs rows)
        elif element_type == "conditional":
            is_row = "row" in element or ".entities[" in path

            if is_row:
                # Conditional row inside an entities card
                if "row" not in element or not isinstance(element["row"], (dict, str)):
                    violations.append(
                        f"{dashboard_path} at '{path}': conditional row inside entities card is missing mandatory 'row:' mapping."
                    )
            else:
                # Conditional card (wrapper)
                # CRITICAL RULE 11 CHECK: Sibling indentation error detection
                if "entities" in element:
                    violations.append(
                        f"RULE 11 INDENTATION VIOLATION in {dashboard_path} at '{path}': "
                        f"Found 'entities:' directly under 'type: conditional'! In Home Assistant Lovelace YAML, "
                        f"'entities:' must be nested strictly under 'card:' (e.g. 10 spaces instead of 8 spaces)."
                    )
                if "cards" in element:
                    violations.append(
                        f"RULE 11 INDENTATION VIOLATION in {dashboard_path} at '{path}': "
                        f"Found 'cards:' directly under 'type: conditional'! In Home Assistant Lovelace YAML, "
                        f"nested cards must be inside 'card:' mapping."
                    )
                if "card" not in element or not isinstance(element["card"], dict):
                    violations.append(
                        f"{dashboard_path} at '{path}': conditional card is missing mandatory 'card:' mapping."
                    )

            if "conditions" not in element or not isinstance(element["conditions"], list):
                violations.append(
                    f"{dashboard_path} at '{path}': conditional element is missing mandatory 'conditions:' list."
                )

        # 3. Validate container cards (stacks)
        elif element_type in ("vertical-stack", "horizontal-stack"):
            if "cards" not in element or not isinstance(element["cards"], list) or len(element["cards"]) == 0:
                violations.append(
                    f"{dashboard_path} at '{path}': '{element_type}' is missing mandatory non-empty 'cards:' list."
                )

        # 4. Validate grid cards
        elif element_type == "grid":
            if "cards" not in element or not isinstance(element["cards"], list) or len(element["cards"]) == 0:
                violations.append(
                    f"{dashboard_path} at '{path}': 'type: grid' is missing mandatory non-empty 'cards:' list."
                )

    return violations


def test_all_dashboard_yaml_files_load():
    """Verify that every dashboard YAML file exists and loads without parsing errors."""
    dashboard_files = sorted(glob.glob(os.path.join(DASHBOARDS_DIR, "*.yaml")))
    assert len(dashboard_files) > 0, f"No dashboard files found in {DASHBOARDS_DIR}"

    for fpath in dashboard_files:
        with open(fpath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data is not None, f"Failed to load dashboard: {fpath} returned None"
        assert "views" in data, f"Dashboard {fpath} must define a 'views' list"


def test_dashboard_schemas_and_indentation_invariants():
    """
    Enforces Rule 11 across all dashboard files in config/dashboards/*.yaml:
    - All 'type: entities' cards must have non-empty 'entities:' lists.
    - All 'type: conditional' cards must never have sibling 'entities:' or 'cards:' keys.
    - All stacks and grids must contain non-empty 'cards:' lists.
    """
    dashboard_files = sorted(glob.glob(os.path.join(DASHBOARDS_DIR, "*.yaml")))
    all_violations = []

    for fpath in dashboard_files:
        rel_path = os.path.relpath(fpath, REPO_ROOT)
        with open(fpath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        violations = validate_dashboard_cards(rel_path, data)
        all_violations.extend(violations)

    assert not all_violations, (
        "LOVELACE DASHBOARD SCHEMA VIOLATIONS (Rule 11):\n" +
        "\n".join(f"  - {v}" for v in all_violations)
    )


def test_regression_catches_sibling_entities_indentation_bug():
    """
    Synthetic regression test reproducing the exact 8-space vs 10-space
    indentation bug that caused Home Assistant to report 'Błąd konfiguracji (No entities specified)'.
    Verifies that validate_dashboard_cards catches the error and reports Rule 11.
    """
    # Flawed YAML where entities: is indented at 8 spaces (sibling of card:)
    flawed_yaml = """
    views:
      - title: Test View
        cards:
          - type: conditional
            conditions:
              - entity: binary_sensor.test_sensor
                state: "off"
            card:
              type: entities
              title: Test Title
              show_header_toggle: false
            entities:
              - entity: sensor.test_child
    """
    parsed = yaml.safe_load(flawed_yaml)
    violations = validate_dashboard_cards("synthetic_dashboard.yaml", parsed)

    assert len(violations) >= 1, "Expected validator to catch sibling entities indentation error!"
    assert any("RULE 11 INDENTATION VIOLATION" in v for v in violations), (
        f"Expected violation message to cite RULE 11 INDENTATION VIOLATION, got: {violations}"
    )
    assert any("is missing mandatory 'entities:'" in v for v in violations), (
        f"Expected nested entities card to be flagged for missing entities, got: {violations}"
    )


def test_valid_conditional_wrapping_entities_passes():
    """
    Verifies that properly indented conditional cards wrapping entities cards
    (10 spaces, nested under card:) pass validation without errors.
    """
    valid_yaml = """
    views:
      - title: Test View
        cards:
          - type: conditional
            conditions:
              - entity: binary_sensor.test_sensor
                state: "off"
            card:
              type: entities
              title: Test Title
              show_header_toggle: false
              entities:
                - entity: sensor.test_child
    """
    parsed = yaml.safe_load(valid_yaml)
    violations = validate_dashboard_cards("synthetic_dashboard.yaml", parsed)
    assert violations == [], f"Expected zero violations for valid YAML, got: {violations}"
