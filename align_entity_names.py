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

# Comprehensive Multi-Attribute Matching Rules for Lighting Entities
LIGHT_RULES = [
    # --- Exact ID Matches (highest priority) ---
    {
        "id_exact": ["light.salon_mqtt"],
        "clean_name": "Salon Główne",
        "aliases": ["światło w salonie", "lampa w salonie", "salon główne", "światło salon"]
    },
    {
        "id_exact": ["light.shellyrgbw2_salon"],
        "clean_name": "Salon LED",
        "aliases": ["led w salonie", "pasek led w salonie", "ledy salon", "podświetlenie salon"]
    },
    {
        "id_exact": ["light.shellyrgbw2_814843"],
        "clean_name": "Kuchnia LED",
        "aliases": ["led w kuchni", "pasek led w kuchni", "ledy kuchnia"]
    },
    {
        "id_exact": ["light.bulb_1"],
        "clean_name": "Taras Lampa Lewa",
        "aliases": ["taras lampa lewa", "taras strona 1", "światło taras lewe", "lampa lewa taras"]
    },
    {
        "id_exact": ["light.bulb_2"],
        "clean_name": "Taras Lampa Prawa",
        "aliases": ["taras lampa prawa", "taras strona 2", "światło taras prawe", "lampa prawa taras"]
    },
    {
        "id_exact": ["light.520a_infra_red_lights_in_night_mode"],
        "clean_name": "Podczerwień Kamery Front",
        "aliases": ["podczerwień kamery", "ir kamery"]
    },

    # --- Peryferia: Gniazdka, Kamery, WiFi ---
    {
        "id_contains": ["gniazdko"],
        "name_contains": ["gniazdko światło"],
        "clean_name": "Duża Łazienka Kinkiet",
        "aliases": ["kinkiet duża łazienka", "lustro w dużej łazience", "duża łazienka kinkiet", "gniazdko w dużej łazience"]
    },
    {
        "id_contains": ["5 in 1", "5in1"],
        "name_contains": ["5 in 1 controller", "5 in 1"],
        "clean_name": "LED Dekoracyjny Multi",
        "aliases": ["pasek led multi", "taśma led multi", "kontroler led", "pasek led"]
    },
    {
        "id_contains": ["reolink_duo_floodlight"],
        "name_contains": ["reolink duo floodlight", "reflektor poe"],
        "clean_name": "Reflektor Kamera Wejście",
        "aliases": ["reflektor kamera wejście", "reflektor przy wejściu", "reflektor wejście", "floodlight"]
    },

    # --- Shelly Plus RGBW PM Salon (Kanały 0-3) ---
    {
        "id_ends": ["_light_0"],
        "name_contains": ["shellyplusrgbwpm-9451dc0a83b4 light 0", "salon dekoracje kanał 1"],
        "orig_exact": ["Light 0"],
        "clean_name": "Salon Dekoracje Kanał 1",
        "aliases": ["salon dekoracje 1", "dekoracje w salonie kanał 1"]
    },
    {
        "id_ends": ["_light_1"],
        "name_contains": ["shellyplusrgbwpm-9451dc0a83b4 light 1", "salon dekoracje kanał 2"],
        "orig_exact": ["Light 1"],
        "clean_name": "Salon Dekoracje Kanał 2",
        "aliases": ["salon dekoracje 2", "dekoracje w salonie kanał 2"]
    },
    {
        "id_ends": ["_light_2"],
        "name_contains": ["shellyplusrgbwpm-9451dc0a83b4 light 2", "salon dekoracje kanał 3"],
        "orig_exact": ["Light 2"],
        "clean_name": "Salon Dekoracje Kanał 3",
        "aliases": ["salon dekoracje 3", "dekoracje w salonie kanał 3"]
    },
    {
        "id_ends": ["_light_3"],
        "name_contains": ["shellyplusrgbwpm-9451dc0a83b4 light 3", "salon dekoracje kanał 4"],
        "orig_exact": ["Light 3"],
        "clean_name": "Salon Dekoracje Kanał 4",
        "aliases": ["salon dekoracje 4", "dekoracje w salonie kanał 4"]
    },

    # --- Shelly RGBW2 Mała Łazienka (Kanały 1-4) ---
    {
        "id_exact": ["light.shellyrgbw2_c8c9a3399f35"],
        "name_contains": ["shelly mała łazienka channel 1", "mała łazienka led kanał 1"],
        "clean_name": "Mała Łazienka LED Kanał 1",
        "aliases": ["led mała łazienka 1", "ledy mała łazienka 1", "ledy w małej łazience 1"]
    },
    {
        "id_ends": ["c8c9a3399f35_channel_2"],
        "name_contains": ["shelly mała łazienka channel 2", "mała łazienka led kanał 2"],
        "clean_name": "Mała Łazienka LED Kanał 2",
        "aliases": ["led mała łazienka 2", "ledy mała łazienka 2", "ledy w małej łazience 2"]
    },
    {
        "id_ends": ["c8c9a3399f35_channel_3", "channel_3"],
        "name_contains": ["shelly mała łazienka channel 3", "mała łazienka led kanał 3"],
        "clean_name": "Mała Łazienka LED Kanał 3",
        "aliases": ["led mała łazienka 3", "ledy mała łazienka 3", "ledy w małej łazience 3"]
    },
    {
        "id_ends": ["c8c9a3399f35_channel_4", "channel_4"],
        "name_contains": ["shelly mała łazienka channel 4", "mała łazienka led kanał 4"],
        "clean_name": "Mała Łazienka LED Kanał 4",
        "aliases": ["led mała łazienka 4", "ledy mała łazienka 4", "ledy w małej łazience 4"]
    },

    # --- BoneIO Dimmer LED 2c7fbc (Kanały CHL 01-04, CHR 01-04) ---
    {
        "id_ends": ["_chl_01"],
        "orig_exact": ["CHL 01"],
        "name_contains": ["chl 01", "korytarz wejściowy"],
        "clean_name": "Korytarz Wejściowy",
        "aliases": ["korytarz wejście", "światło w holu", "hol wejściowy", "przedpokój"]
    },
    {
        "id_ends": ["_chl_02"],
        "orig_exact": ["CHL 02"],
        "name_contains": ["chl 02", "korytarz sypialnie"],
        "clean_name": "Korytarz Sypialnie",
        "aliases": ["korytarz sypialnia", "korytarz przy sypialniach", "hol sypialnie"]
    },
    {
        "id_ends": ["_chl_04"],
        "orig_exact": ["CHL 04"],
        "name_contains": ["chl 04", "kuchnia główne"],
        "clean_name": "Kuchnia Główne",
        "aliases": ["światło w kuchni", "kuchnia", "kuchnia główne", "zgaś kuchnię", "włącz kuchnię"]
    },
    {
        "id_ends": ["_chr_01"],
        "orig_exact": ["CHR 01"],
        "name_contains": ["chr 01", "korytarz ściana"],
        "clean_name": "Korytarz Ściana",
        "aliases": ["korytarz ściana", "kinkiety w korytarzu", "podświetlenie ściany w korytarzu"]
    },
    {
        "id_ends": ["_chr_02"],
        "orig_exact": ["CHR 02"],
        "name_contains": ["chr 02", "mała łazienka dekor", "mała łazienka dekoracyjne"],
        "clean_name": "Mała Łazienka Dekoracyjne",
        "aliases": ["lustro w małej łazience", "mała łazienka lustro", "dekoracje mała łazienka", "kinkiet mała łazienka"]
    },
    {
        "id_ends": ["_chr_04"],
        "orig_exact": ["CHR 04"],
        "name_contains": ["chr 04", "kuchnia blat"],
        "clean_name": "Kuchnia Blat",
        "aliases": ["blat w kuchni", "kuchnia blat", "pod szafkami w kuchni", "kuchnia prawa"]
    },

    # --- BoneIO ESP 32x10 Relays (Light 01-22, Door 23) ---
    {
        "id_ends": ["_light_01"],
        "orig_exact": ["Light 01"],
        "name_contains": ["wejście światło 01", "wiatrołap główne"],
        "clean_name": "Wiatrołap Główne",
        "aliases": ["wiatrołap", "światło w wiatrołapie", "wejście", "światło przy wejściu"]
    },
    {
        "id_ends": ["_light_02"],
        "orig_exact": ["Light 02"],
        "name_contains": ["gospodarcze główne"],
        "clean_name": "Gospodarcze Główne",
        "aliases": ["gospodarcze", "światło w gospodarczym", "pomieszczenie gospodarcze", "kotłownia"]
    },
    {
        "id_ends": ["_light_03"],
        "orig_exact": ["Light 03"],
        "name_contains": ["hol wejściowy główne"],
        "clean_name": "Hol Wejściowy Główne",
        "aliases": ["hol wejściowy sufit", "światło w holu sufit", "przedpokój sufit"]
    },
    {
        "id_ends": ["_light_04"],
        "orig_exact": ["Light 04"],
        "name_contains": ["klatka schodowa kinkiety"],
        "clean_name": "Klatka Schodowa Kinkiety",
        "aliases": ["klatka schodowa", "kinkiety na schodach", "schody kinkiety"]
    },
    {
        "id_ends": ["_light_05"],
        "orig_exact": ["Light 05"],
        "name_contains": ["kuchnia wyspa"],
        "clean_name": "Kuchnia Wyspa",
        "aliases": ["wyspa", "wyspa w kuchni", "światło nad wyspą", "kuchnia wyspa"]
    },
    {
        "id_ends": ["_light_06"],
        "orig_exact": ["Light 06"],
        "name_contains": ["jadalnia", "jadalnia główne"],
        "clean_name": "Jadalnia Główne",
        "aliases": ["jadalnia", "stół w jadalni", "światło nad stołem", "światło w jadalni"]
    },
    {
        "id_ends": ["_light_07"],
        "orig_exact": ["Light 07"],
        "name_contains": ["sypialnia zasilanie", "sypialnia garderoba"],
        "clean_name": "Sypialnia Garderoba",
        "aliases": ["garderoba", "światło w garderobie", "szafa w sypialni", "garderoba w sypialni"]
    },
    {
        "id_ends": ["_light_08"],
        "orig_exact": ["Light 08"],
        "name_contains": ["sypialnia dekoracyjne"],
        "clean_name": "Sypialnia Dekoracyjne",
        "aliases": ["światło dekoracyjne w sypialni", "dekoracje sypialnia", "sypialnia kinkiety"]
    },
    {
        "id_ends": ["_light_09"],
        "orig_exact": ["Light 09"],
        "name_contains": ["pralnia", "pralnia główne"],
        "clean_name": "Pralnia Główne",
        "aliases": ["pralnia", "światło w pralni", "pomieszczenie gospodarcze"]
    },
    {
        "id_ends": ["_light_10"],
        "orig_exact": ["Light 10"],
        "name_contains": ["duża łazienka główne", "łazienka duża"],
        "clean_name": "Duża Łazienka Główne",
        "aliases": ["duża łazienka", "światło w dużej łazience", "duża łazienka główne", "łazienka na piętrze", "łazienka na górze"]
    },
    {
        "id_ends": ["_light_11"],
        "orig_exact": ["Light 11"],
        "name_contains": ["22 ogród taras zasilanie", "taras zasilanie", "zasilanie taras"],
        "clean_name": "Taras Zasilanie",
        "aliases": ["zasilanie taras", "taras", "światło na tarasie", "włącz taras"]
    },
    {
        "id_ends": ["_light_12"],
        "orig_exact": ["Light 12"],
        "name_contains": ["light 12 - ogród", "podjazd", "podjazd główne"],
        "clean_name": "Podjazd Główne",
        "aliases": ["podjazd", "światło na podjeździe", "oświetlenie podjazdu", "latarnia podjazd"]
    },
    {
        "id_ends": ["_light_13"],
        "orig_exact": ["Light 13"],
        "name_contains": ["ogród elewacja"],
        "clean_name": "Ogród Elewacja",
        "aliases": ["ogród kinkiety", "światło w ogrodzie", "oświetlenie ogrodu", "kinkiety elewacja"]
    },
    {
        "id_ends": ["_light_14"],
        "orig_exact": ["Light 14"],
        "name_contains": ["nikola główne", "pokój nikoli główne"],
        "clean_name": "Pokój Nikoli Główne",
        "aliases": ["pokój Nikoli", "u Nikoli", "światło u Nikoli", "Nikola"]
    },
    {
        "id_ends": ["_light_15"],
        "orig_exact": ["Light 15"],
        "name_contains": ["hol sypialnie główne"],
        "clean_name": "Hol Sypialnie Główne",
        "aliases": ["hol sypialnie sufit", "korytarz piętro", "światło na piętrze", "hol na piętrze"]
    },
    {
        "id_ends": ["_light_16"],
        "orig_exact": ["Light 16"],
        "name_contains": ["klara główne", "pokój klary główne"],
        "clean_name": "Pokój Klary Główne",
        "aliases": ["pokój Klary", "u Klary", "światło u Klary", "Klara"]
    },
    {
        "id_ends": ["_light_17"],
        "orig_exact": ["Light 17"],
        "name_contains": ["sypialnia główne"],
        "clean_name": "Sypialnia Główne",
        "aliases": ["światło w sypialni", "lampa w sypialni", "sypialnia główne", "sypialnia"]
    },
    {
        "id_ends": ["_light_18"],
        "orig_exact": ["Light 18"],
        "name_contains": ["zasilanie ścianka salon 18", "salon dodatkowe"],
        "clean_name": "Salon Dodatkowe",
        "aliases": ["dodatkowe w salonie", "światło dodatkowe salon", "boczne w salonie", "kinkiety w salonie"]
    },
    {
        "id_ends": ["_light_19"],
        "orig_exact": ["Light 19"],
        "name_contains": ["gabinet nocne", "gabinet", "gabinet główne"],
        "clean_name": "Gabinet Główne",
        "aliases": ["gabinet", "światło w gabinecie", "biuro", "gabinet główne", "biuro główne"]
    },
    {
        "id_ends": ["_light_20"],
        "orig_exact": ["Light 20"],
        "name_contains": ["20 łazienka mała", "zasilanie łazienka m", "mała łazienka główne"],
        "clean_name": "Mała Łazienka Główne",
        "aliases": ["światło w małej łazience", "mała łazienka", "mała łazienka główne", "dolna łazienka", "zgaś małą łazienkę", "włącz małą łazienkę"]
    },
    {
        "id_ends": ["_light_21"],
        "orig_exact": ["Light 21"],
        "name_contains": ["taras kinkiety"],
        "clean_name": "Taras Kinkiety",
        "aliases": ["taras kinkiety", "światło ścienne taras", "kinkiety taras", "kinkiety na tarasie"]
    },
    {
        "id_ends": ["_light_22"],
        "orig_exact": ["Light 22"],
        "name_contains": ["ogród główne"],
        "clean_name": "Ogród Główne",
        "aliases": ["ogród", "światło w ogrodzie", "oświetlenie ogrodu", "ogród lampy"]
    },
    {
        "id_ends": ["_door_23_relay", "door_23"],
        "orig_exact": ["Door 23 Relay"],
        "name_contains": ["door 23 relay", "rygiel drzwi", "rygiel drzwi wejściowych"],
        "clean_name": "Rygiel Drzwi Wejściowych",
        "aliases": ["otwórz drzwi", "rygiel drzwi", "domofon", "drzwi rygiel"]
    }
]


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
    """Checks if an entity matches any mapping with multi-attribute priority."""
    ent_id = (entry.get("entity_id") or "").lower()
    name = (entry.get("name") or "").lower()
    orig_name = (entry.get("original_name") or "").lower()

    domain = ent_id.split(".")[0] if "." in ent_id else ""
    if domain not in ("light", "switch"):
        return None

    # Priority 1: Exact ID match
    for rule in LIGHT_RULES:
        if "id_exact" in rule:
            for ie in rule["id_exact"]:
                if ent_id == ie.lower():
                    return rule["clean_name"], rule["aliases"]

    # Priority 2: Suffix ID match (for BoneIO relays and Dimmer/Shelly channels)
    for rule in LIGHT_RULES:
        if "id_ends" in rule:
            for ie in rule["id_ends"]:
                if ent_id.endswith(ie.lower()):
                    return rule["clean_name"], rule["aliases"]

    # Priority 3: Substring in ID (e.g. gniazdko, reolink, 5 in 1)
    for rule in LIGHT_RULES:
        if "id_contains" in rule:
            for ic in rule["id_contains"]:
                if ic.lower() in ent_id:
                    return rule["clean_name"], rule["aliases"]

    # Priority 4: Specific name contains (e.g. installer names, custom UI names)
    for rule in LIGHT_RULES:
        if "name_contains" in rule:
            for nc in rule["name_contains"]:
                if nc.lower() in name:
                    return rule["clean_name"], rule["aliases"]

    # Priority 5: Exact original_name match (ESPHome defaults)
    for rule in LIGHT_RULES:
        if "orig_exact" in rule:
            for oe in rule["orig_exact"]:
                if orig_name == oe.lower():
                    return rule["clean_name"], rule["aliases"]

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
