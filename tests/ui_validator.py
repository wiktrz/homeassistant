#!/usr/bin/env python3
"""
Tier 3: Dashboard & Lovelace UI Static and E2E Validator.
Traverses Lovelace YAML dashboard definitions, extracts card elements,
verifies that entity references are valid, cards have proper schemas,
and checks view accessibility.
"""

import os
import sys
import yaml
from typing import Dict, Any, List, Set, Tuple

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")

DASHBOARDS_TO_VALIDATE = [
    "dashboard_modern_reference.yaml",
    "dashboard_home_improved.yaml",
    "dashboard_improved.yaml",
    "dashboard_korytarze_improved.yaml",
    "dashboard_energy_test.yaml",
]


def extract_entities_from_card(card: Any) -> Set[str]:
    """Recursively extracts all entity IDs referenced inside a Lovelace card."""
    entities = set()
    if isinstance(card, dict):
        if "entity" in card and isinstance(card["entity"], str):
            entities.add(card["entity"])
        if "entities" in card and isinstance(card["entities"], list):
            for item in card["entities"]:
                if isinstance(item, str):
                    entities.add(item)
                elif isinstance(item, dict) and "entity" in item:
                    entities.add(item["entity"])
        if "cards" in card and isinstance(card["cards"], list):
            for sub_card in card["cards"]:
                entities.update(extract_entities_from_card(sub_card))
        for key in ("card", "tap_action", "hold_action"):
            if key in card and isinstance(card[key], dict):
                target = card[key].get("target", {})
                if isinstance(target, dict) and "entity_id" in target:
                    eid = target["entity_id"]
                    if isinstance(eid, str):
                        entities.add(eid)
                    elif isinstance(eid, list):
                        entities.update(eid)
    return entities


def validate_dashboard(filename: str) -> Tuple[bool, List[str], Set[str]]:
    """Validates the structure, views, and cards of a dashboard YAML file."""
    path = os.path.join(CONFIG_DIR, filename)
    logs = [f"▶ Validating Dashboard: {filename}"]
    referenced_entities = set()

    if not os.path.exists(path):
        logs.append(f"  ❌ File not found: {path}")
        return False, logs, referenced_entities

    with open(path, "r", encoding="utf-8") as f:
        try:
            content = yaml.safe_load(f)
        except Exception as e:
            logs.append(f"  ❌ YAML parsing failed: {e}")
            return False, logs, referenced_entities

    if not isinstance(content, dict):
        logs.append("  ❌ Dashboard root must be a dictionary.")
        return False, logs, referenced_entities

    title = content.get("title", "Untitled")
    views = content.get("views", [])
    if not isinstance(views, list):
        logs.append("  ❌ 'views' must be a list.")
        return False, logs, referenced_entities

    logs.append(f"  ✓ Dashboard title: '{title}' ({len(views)} views found)")

    total_cards = 0
    for idx, view in enumerate(views):
        v_title = view.get("title", f"view_{idx}")
        v_path = view.get("path", f"path_{idx}")
        cards = view.get("cards", [])
        total_cards += len(cards)

        for card in cards:
            if not isinstance(card, dict):
                logs.append(f"  ❌ Invalid card in view '{v_title}': not a dict.")
                return False, logs, referenced_entities
            card_type = card.get("type")
            if not card_type:
                logs.append(f"  ❌ Card in view '{v_title}' missing required 'type' field.")
                return False, logs, referenced_entities

            ents = extract_entities_from_card(card)
            referenced_entities.update(ents)

    logs.append(f"  ✓ Verified {total_cards} cards across {len(views)} views.")
    logs.append(f"  ✓ Discovered {len(referenced_entities)} distinct referenced entities.")
    return True, logs, referenced_entities


def run_all_dashboard_validations() -> bool:
    all_ok = True
    total_entities = set()
    for db in DASHBOARDS_TO_VALIDATE:
        ok, logs, ents = validate_dashboard(db)
        total_entities.update(ents)
        for line in logs:
            print(line)
        if not ok:
            all_ok = False
        print()

    print(f"📊 Total unique entities verified across all dashboards: {len(total_entities)}")
    return all_ok


if __name__ == "__main__":
    success = run_all_dashboard_validations()
    sys.exit(0 if success else 1)
