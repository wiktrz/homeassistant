#!/usr/bin/env python3
"""
Automated Entity Registry & Customize Alignment Script for Home Assistant
Updates entity names and voice assistant aliases directly in Home Assistant's
.storage/core.entity_registry and keeps config/customize.yaml in sync.

Usage on Raspberry Pi (from the homeassistant directory):
  docker stop homeassistant
  python3 align_entity_names.py
  docker start homeassistant
"""

import json
import os
import shutil
import sys
from datetime import datetime

try:
    import yaml
except ImportError:
    yaml = None

# Define target mappings: search terms (matches name, original_name, or entity_id) -> (clean_name, aliases)
LIGHT_MAPPINGS = {
    # BoneIO relays (73bbd8) & installer names
    "20 Łazienka Mała": ("Mała Łazienka Główne", ["światło w małej łazience", "mała łazienka", "mała łazienka główne", "dolna łazienka"]),
    "Zasilanie Łazienka M": ("Mała Łazienka Główne", ["światło w małej łazience", "mała łazienka", "mała łazienka główne", "dolna łazienka"]),
    "Lustro Korytarz": ("Mała Łazienka Dekoracyjne", ["lustro w małej łazience", "mała łazienka lustro", "kinkiet mała łazienka"]),
    "Łazienka Mała Dekor": ("Mała Łazienka Dekoracyjne", ["lustro w małej łazience", "mała łazienka lustro", "kinkiet mała łazienka"]),
    "Wejście światło 01": ("Wiatrołap Główne", ["wiatrołap", "światło w wiatrołapie", "wejście", "światło przy wejściu"]),
    "Zasilanie ścianka salon 18": ("Salon Dodatkowe", ["dodatkowe w salonie", "światło dodatkowe salon", "boczne w salonie", "kinkiety w salonie"]),
    "Sypialnia Zasilanie": ("Sypialnia Główne", ["światło w sypialni", "lampa w sypialni", "sypialnia główne", "sypialnia"]),
    "Sypialnia Dekoracyjne": ("Sypialnia Dekoracyjne", ["światło dekoracyjne w sypialni", "dekoracje sypialnia", "sypialnia kinkiety"]),
    "22 Ogród Taras Zasilanie": ("Taras Zasilanie", ["zasilanie taras", "taras", "światło na tarasie", "włącz taras"]),
    "Taras Lampa 1": ("Taras Lampa Lewa", ["taras lampa lewa", "taras strona 1", "światło taras lewe"]),
    "Taras Lampa 2": ("Taras Lampa Prawa", ["taras lampa prawa", "taras strona 2", "światło taras prawe"]),
    "Jadalnia": ("Jadalnia Główne", ["jadalnia", "stół w jadalni", "światło nad stołem", "światło w jadalni"]),
    "Klara Główne": ("Pokój Klary Główne", ["pokój Klary", "u Klary", "światło u Klary", "Klara"]),
    "Nikola Główne": ("Pokój Nikoli Główne", ["pokój Nikoli", "u Nikoli", "światło u Nikoli", "Nikola"]),
    "Pralnia": ("Pralnia Główne", ["pralnia", "światło w pralni", "pomieszczenie gospodarcze"]),
    "Gniazdko Światło (Duża łazienka)": ("Duża Łazienka Główne", ["duża łazienka", "światło w dużej łazience", "łazienka na górze", "górna łazienka"]),
    "Shelly salon": ("Salon LED", ["led w salonie", "pasek led w salonie", "ledy salon", "podświetlenie salon"]),
    "Salon Światło": ("Salon Główne", ["światło w salonie", "lampa w salonie", "salon główne", "światło salon"]),
    
    # Shelly Plus RGBW PM (Salon)
    "shellyplusrgbwpm-9451dc0a83b4 Light 0": ("Salon Dekoracje Kanał 1", ["salon dekoracje 1", "dekoracje w salonie kanał 1"]),
    "shellyplusrgbwpm-9451dc0a83b4 Light 1": ("Salon Dekoracje Kanał 2", ["salon dekoracje 2", "dekoracje w salonie kanał 2"]),
    "shellyplusrgbwpm-9451dc0a83b4 Light 2": ("Salon Dekoracje Kanał 3", ["salon dekoracje 3", "dekoracje w salonie kanał 3"]),
    "shellyplusrgbwpm-9451dc0a83b4 Light 3": ("Salon Dekoracje Kanał 4", ["salon dekoracje 4", "dekoracje w salonie kanał 4"]),
    
    # Shelly RGBW2 (Mała łazienka)
    "Shelly mała łazienka channel 1": ("Mała Łazienka LED Kanał 1", ["led mała łazienka 1", "ledy mała łazienka"]),
    "Shelly mała łazienka channel 2": ("Mała Łazienka LED Kanał 2", ["led mała łazienka 2"]),
    
    # BoneIO Dimmers
    "BoneIO Dimmer LED 2c7fbc CHL 01": ("Korytarz Wejściowy", ["korytarz wejście", "światło w holu", "hol wejściowy", "przedpokój"]),
    "BoneIO Dimmer LED 2c7fbc CHL 02": ("Korytarz Sypialnie", ["korytarz sypialnia", "korytarz przy sypialniach", "hol sypialnie"]),
    "BoneIO Dimmer LED 2c7fbc CHL 04": ("Kuchnia Główne", ["światło w kuchni", "kuchnia", "kuchnia główne", "zgaś kuchnię", "włącz kuchnię"]),
    "BoneIO Dimmer LED 2c7fbc CHR 01": ("Korytarz Ściana", ["korytarz ściana", "kinkiety w korytarzu", "podświetlenie korytarza"]),
    "BoneIO Dimmer LED 2c7fbc CHR 02": ("Mała Łazienka Dekoracyjne", ["lustro w małej łazience", "mała łazienka lustro"]),
    "BoneIO Dimmer LED 2c7fbc CHR 04": ("Kuchnia Blat", ["blat w kuchni", "pod szafkami w kuchni", "kuchnia prawa", "kuchnia blat"]),
    
    # BoneIO Specific Relays
    "BoneIO ESP 32x10 Lights 73bbd8 Light 01": ("Wiatrołap Główne", ["wiatrołap", "światło w wiatrołapie", "wejście"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 05": ("Kuchnia Wyspa", ["wyspa", "wyspa w kuchni", "światło nad wyspą"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 06": ("Jadalnia Główne", ["jadalnia", "stół w jadalni", "światło nad stołem"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 07": ("Sypialnia Główne", ["światło w sypialni", "lampa w sypialni", "sypialnia"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 08": ("Sypialnia Dekoracyjne", ["światło dekoracyjne w sypialni", "dekoracje sypialnia"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 09": ("Pralnia Główne", ["pralnia", "światło w pralni"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 10": ("Duża Łazienka Główne", ["duża łazienka", "światło w dużej łazience", "łazienka na piętrze"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 11": ("Taras Zasilanie", ["zasilanie taras", "taras", "światło na tarasie"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 12": ("Podjazd Główne", ["podjazd", "światło na podjeździe", "oświetlenie podjazdu"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 14": ("Pokój Nikoli Główne", ["pokój Nikoli", "u Nikoli", "światło u Nikoli"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 16": ("Pokój Klary Główne", ["pokój Klary", "u Klary", "światło u Klary"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 17": ("Sypialnia Główne", ["światło w sypialni", "lampa w sypialni", "sypialnia"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 18": ("Salon Dodatkowe", ["dodatkowe w salonie", "boczne w salonie"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 19": ("Gabinet Nocne", ["gabinet nocne", "biuro nocne", "światło w gabinecie"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 20": ("Mała Łazienka Główne", ["światło w małej łazience", "mała łazienka", "mała łazienka główne", "dolna łazienka"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Light 22": ("Taras Zasilanie", ["zasilanie taras", "taras", "światło na tarasie"]),
    "BoneIO ESP 32x10 Lights 73bbd8 Door 23 Relay": ("Rygiel Drzwi Wejściowych", ["otwórz drzwi", "rygiel drzwi", "domofon"]),
}


def find_entity_registry():
    """Locates core.entity_registry file."""
    candidates = [
        "config/.storage/core.entity_registry",
        "/config/.storage/core.entity_registry",
        ".storage/core.entity_registry",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def find_customize_yaml():
    """Locates customize.yaml file."""
    candidates = [
        "config/customize.yaml",
        "/config/customize.yaml",
        "customize.yaml",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def match_target(entry):
    """Checks if an entity matches any mapping."""
    ent_id = entry.get("entity_id", "")
    name = entry.get("name") or ""
    orig_name = entry.get("original_name") or ""

    domain = ent_id.split(".")[0] if "." in ent_id else ""
    if domain not in ("light", "switch"):
        return None

    for search_term, (clean_name, aliases) in LIGHT_MAPPINGS.items():
        search_lower = search_term.lower()
        if (
            search_lower == name.lower()
            or search_lower == orig_name.lower()
            or search_lower in name.lower()
            or search_lower in orig_name.lower()
        ):
            return clean_name, aliases

    return None


def update_registry():
    reg_path = find_entity_registry()
    if not reg_path:
        print("[-] ERROR: Could not find core.entity_registry.")
        print("    Ensure you run this script from the homeassistant root folder or with config/ present.")
        sys.exit(1)

    print(f"[+] Found entity registry at: {reg_path}")

    # Create backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{reg_path}.bak_{timestamp}"
    shutil.copy2(reg_path, backup_path)
    print(f"[+] Backup created at: {backup_path}")

    with open(reg_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entities = data.get("data", {}).get("entities", [])
    updated_count = 0
    discovered_ids = {}

    for entry in entities:
        ent_id = entry.get("entity_id")
        current_name = entry.get("name")
        orig_name = entry.get("original_name")

        # 1. Match lighting entities
        target = match_target(entry)
        if target:
            new_name, new_aliases = target
            existing_aliases = entry.get("aliases") or []
            merged_aliases = list(dict.fromkeys(existing_aliases + new_aliases))

            if current_name != new_name or entry.get("aliases") != merged_aliases:
                print(f"[>] UPDATING LIGHT: {ent_id}")
                print(f"    Old name: '{current_name}' (original: '{orig_name}')")
                print(f"    New name: '{new_name}'")
                print(f"    Aliases:  {merged_aliases}")
                entry["name"] = new_name
                entry["aliases"] = merged_aliases
                updated_count += 1
                discovered_ids[ent_id] = (new_name, merged_aliases)
            continue

        # 2. Match local00 sensors / climates with local00 in name or original_name
        if current_name and current_name.startswith("local00 "):
            cleaned = current_name.replace("local00 ", "", 1)
            print(f"[>] CLEANING LOCAL00: {ent_id}: '{current_name}' -> '{cleaned}'")
            entry["name"] = cleaned
            updated_count += 1
        elif not current_name and orig_name and orig_name.startswith("local00 "):
            cleaned = orig_name.replace("local00 ", "", 1)
            print(f"[>] SETTING CLEAN NAME: {ent_id}: (orig: '{orig_name}') -> '{cleaned}'")
            entry["name"] = cleaned
            updated_count += 1

    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n[+] SUCCESS: Updated {updated_count} entities in {reg_path}")

    # Synchronize discovered IDs into customize.yaml as well
    cust_path = find_customize_yaml()
    if cust_path and discovered_ids and yaml:
        try:
            with open(cust_path, "r", encoding="utf-8") as f:
                cust_data = yaml.safe_load(f) or {}
            
            for eid, (cname, caliases) in discovered_ids.items():
                if eid not in cust_data:
                    cust_data[eid] = {}
                cust_data[eid]["friendly_name"] = cname
                cust_data[eid]["aliases"] = caliases
            
            with open(cust_path, "w", encoding="utf-8") as f:
                yaml.dump(cust_data, f, allow_unicode=True, sort_keys=False)
            print(f"[+] Synchronized {len(discovered_ids)} actual entity IDs into {cust_path}")
        except Exception as e:
            print(f"[!] Warning: Could not update customize.yaml: {e}")

    print("\n[+] To apply changes in Home Assistant, run:")
    print("    docker restart homeassistant\n")


if __name__ == "__main__":
    update_registry()
