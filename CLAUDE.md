# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Mandatory rules (single source of truth, imported):
@AI_DEVELOPMENT_GUIDELINES.md

Project index & architecture map (imported):
@llms.txt

> **MUST read first** for any documentation or implementation task (not imported, 51 KB): `AGENT.md` (unified HA & ESP32/Node.js ecosystem bridge).
> Also consult `PLACES_AND_BEHAVIORS.md` (room-by-room entity/behavior matrix) before touching entities, and `AGENTS.md` (Codex instructions, kept in sync with AGENT.md).

## Core AI Operating Rules

All rule text lives in `AI_DEVELOPMENT_GUIDELINES.md` (imported above). Do not duplicate it here; these are pointers only.

1. Rule 1 (see AI_DEVELOPMENT_GUIDELINES.md): planning/design/review mode is read-only; only an explicit user command unlocks edits.
2. Rule 2 (see AI_DEVELOPMENT_GUIDELINES.md): every code/config change syncs `PLACES_AND_BEHAVIORS.md`, `AGENT.md`, `AGENTS.md`, `llms.txt` (and `CLAUDE.md` for CLI/env changes).
3. Rule 3 (see AI_DEVELOPMENT_GUIDELINES.md): never `git add`/`commit`/`push` without the explicit words 'commit'/'push'; leave changes unstaged.
4. Rule 4 (see AI_DEVELOPMENT_GUIDELINES.md): start by reading `AGENT.md`; check `PLACES_AND_BEHAVIORS.md` before adding/modifying entities.
5. Rule 5 (see AI_DEVELOPMENT_GUIDELINES.md): never clear optimization databases or caches.
6. Rule 6 (see AI_DEVELOPMENT_GUIDELINES.md): `python3 tests/runner.py --auto` must pass; new features get a scenario in `tests/scenarios/` registered in `tests/test_registry.yaml`.
7. Rule 7 (see AI_DEVELOPMENT_GUIDELINES.md): end every substantive session with the structured retrospective and re-validation.
8. Rule 8 (see AI_DEVELOPMENT_GUIDELINES.md): English-only identifiers; Polish only in user-facing UI/TTS/notifications.
9. Rule 9 (see AI_DEVELOPMENT_GUIDELINES.md): zero hardcoded battery/inverter hardware parameters (see `SPEC_DEYE_BATTERY_SYSTEM.md`).
10. Rule 10 (see AI_DEVELOPMENT_GUIDELINES.md): fallbacks live only in `config/.env.example` (single template; real `config/.env`, no root copies); run `--search` before adding variables, `--validate` must pass.
11. Rule 11 (see AI_DEVELOPMENT_GUIDELINES.md): Lovelace conditional cards nest `entities:`/`cards:` under `card:`; no empty lists.

## Definition of Done (Claude)

A task that changed files is done only when every item holds:

- [ ] `python3 tests/runner.py --auto` passes for the current diff (Rule 6).
- [ ] `python3 scripts/check_duplicate_variable.py --search "<concept>"` was run before adding any constant/env var/helper, and `python3 scripts/check_duplicate_variable.py --validate` exits 0 (Rule 10).
- [ ] Docs synced: `PLACES_AND_BEHAVIORS.md`, `AGENT.md`, `AGENTS.md`, `llms.txt`, plus `SPEC_DEYE_BATTERY_SYSTEM.md` when battery/inverter logic changes (Rule 2).
- [ ] New scenarios are registered in `tests/test_registry.yaml` (Rule 6).
- [ ] Retrospective done (Rule 7).
- [ ] Changes left unstaged (Rule 3).
- [ ] With the `dev-team` skill: every subagent prompt carries these rules and this checklist.

`/verify` (`.claude/commands/verify.md`) runs the whole checklist and reports a pass/fail table.

**Enforced by project hooks** (`.claude/settings.json` -> `.claude/hooks/guard.py`): PreToolUse denies `git add`/`git stage`/`git commit -a`; PostToolUse runs `--validate` after edits to `config/*.py`, `config/.env.example`, `config/configuration.yaml`, `config/templates.yaml`; a successful `tests/runner.py` run is recorded per diff fingerprint in `.claude/state/` (gitignored); the Stop hook blocks once (then only warns) when the runner pass, doc sync or scenario registration is missing.

## Project Overview

This is a Home Assistant deployment project using Docker for home automation control. It integrates with the `home_automation` project which contains ESP32 heating controllers and Node.js backend.

**Key Components:**
- **Home Assistant** - Main UI, automations, MQTT sensors/climate controls
- **Zigbee2MQTT** - Zigbee device integration (Sonoff Zigbee 3.0 USB Dongle Plus V2)
- **BoneIO 32x10 Relay & 8ch PWM Dimmer** - Industrial DIN rail relay and dimmer controllers
- **Pstryk Energy Pricing** - Dynamic energy pricing from Polish provider
- **Home Assistant Voice PE** - Voice satellite device (ESPHome, Nabu Casa) for voice assistant and TTS announcements
- **Multimedia & Salon TCL Google TV** - Streaming app launcher, live camera HLS casting, Cinema Mode

**Location:** `/Users/wtrzonkowski/Desktop/private/homeassistant/`

## Architecture

### Docker Services

| Service | Image | Network | Purpose |
|---------|-------|---------|---------|
| homeassistant | `ghcr.io/home-assistant/home-assistant:stable` | host | Main HA instance |
| zigbee2mqtt | `ghcr.io/koenkk/zigbee2mqtt` | bridge | Zigbee bridge |

### Network Configuration

| Service | Port | Protocol |
|---------|------|----------|
| Home Assistant | 8123 | HTTP |
| Zigbee2MQTT Frontend | 8124 | HTTP |
| Zigbee2MQTT Serial | /dev/ttyUSB0 | UART |

**Note:** Home Assistant uses `network_mode: host` for direct host network access.

## Configuration Structure

```
config/
├── configuration.yaml     # Main HA config (location, units, includes)
├── mqtt.yaml             # MQTT sensors, lights, climate entities
├── automations.yaml      # 60+ automation rules
├── scripts.yaml          # HA scripts (roller, lighting, cinema, radio, TV)
├── scenes.yaml           # Scene definitions
├── templates.yaml        # Template sensors (Pstryk pricing, waste, power switches)
├── customize.yaml        # Entity customization
├── secrets.yaml          # Sensitive configuration
├── views.yaml            # View definitions
├── dashboard_modern_reference.yaml # Dom iPad Modern dashboard (/dashboard-home)
├── dashboard_improved.yaml       # Main dashboard
├── dashboard_korytarze_improved.yaml  # Hallways dashboard
├── blueprints/           # HA blueprint templates
│   ├── automation/
│   │   ├── motion_light.yaml
│   │   ├── notify_leaving_zone.yaml
│   │   └── script/
│   │       └── confirmable_notification.yaml
│   └── template/
│       └── inverted_binary_sensor.yaml
├── pstryk_pricing.py     # Legacy/unused (pstryk_engine.py is the engine)
└── pstryk_engine.py      # Dynamic energy & metering engine (caching, aggregations, unit-tested)

data/
└── configuration.yaml    # Zigbee2MQTT configuration
```

## MQTT Integration

### MQTT Topics Consumed

**Heating Controller Topics:**
```
heating_controller/{name}/power                    # Power state
heating_controller/{name}/{flat}/{pin}/state     # Pin ON/OFF state
{flat}/thermostat/{pin}/routine                  # Current routine name
{device}/onewire/found                           # Discovered sensors
```

**Thermostat Topics:**
```
local00-{local06}/{Room}/details                 # Temperature/details JSON
local0X/thermostat/{Room}/temperature/set        # Set target temperature
local0X/thermostat/{Room}/routine/set            # Change heating plan
room0X_{room}/sensor/temperature/state           # Temperature readings
```

### Supported Locales

| Locale | Type | Thermostats |
|--------|------|-------------|
| local00 | Primary/Home | Kuchnia, Salon, Sypialnia, Lazienka, MalaLazienka, Klara, Nikola, Gabinet, Gospodarcze, Wiatrolap, HolWejscie, HolSypialnia |
| local01-local06 | Rental Units | Glowny, Lazienka, Sypialnia |

## Zigbee2MQTT Configuration

**Serial Device:** Sonoff Zigbee 3.0 USB Dongle Plus V2
**Adapter:** ember (Silicon Labs EmberZNet)
**Zigbee Channel:** 11
**PAN ID:** 4516

**Key Settings (data/configuration.yaml):**
```yaml
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://localhost:1883
serial:
  adapter: ember
  baudrate: 115200
  rtscts: true
frontend:
  enabled: true
  port: 8124
homeassistant:
  enabled: false  # Using manual MQTT config instead
```

## Automations & Subsystems Overview

### Heating/Routine Automations (18)
- Thermostat routine changes based on input_select for local01-local06

### Lighting Automations & Hardware Architecture
- Motion-triggered lights (PIR sensors)
- Time-based dimming (sun elevation triggers)
- Shelly RGBW animations (continuous loop, cycle, wave, chase, pulse, rock)
- Light Salon MQTT control

**BoneIO Hardware & Entity Mappings:**
- **BoneIO 32x10 Relay Board (`boneio-32-l-07` - 230V Relays):**
  - **Oświetlenie (Lights):**
    - `light.boneio_32_l_07_new_light_01`: Wejście (Wiatrołap Główne)
    - `light.boneio_32_l_07_new_light_05`: **Kuchnia Wyspa** (lampa wisząca nad wyspą)
    - `light.boneio_32_l_07_new_light_06`: Jadalnia (lampa nad stołem)
    - `light.boneio_32_l_07_new_light_12`: Podjazd (oświetlenie podjazdu)
  - **Zasilacze Oświetlenia (Power Supplies - EXCLUDED from All Lights Off scripts):**
    - `light.boneio_32_l_07_new_light_18` / `switch.zasilanie_salon_dodatkowe`: **Zasilanie Salon Dodatkowe**
    - `light.boneio_32_l_07_new_light_07` / `switch.zasilanie_sypialnia_garderoba`: **Zasilanie Sypialnia Garderoba**
    - `light.boneio_32_l_07_new_light_20` / `switch.zasilanie_mala_lazienka`: **Zasilanie Mała Łazienka**
    - `light.boneio_32_l_07_new_light_11` / `switch.zasilanie_taras`: **Zasilanie Taras**
- **BoneIO 8ch LED Dimmer (`boneio-dr-8ch-03-2c7fbc` - 24V PWM Dimmer):**
  - `light.boneio_dr_8ch_03_2c7fbc_chl_01`: Korytarz Wejściowy
  - `light.boneio_dr_8ch_03_2c7fbc_chl_02`: Korytarz Sypialnie
  - `light.boneio_dr_8ch_03_2c7fbc_chl_04`: Kuchnia Główne
  - `light.boneio_dr_8ch_03_2c7fbc_chr_01`: Korytarz Ściana
  - `light.boneio_dr_8ch_03_2c7fbc_chr_02`: Mała Łazienka Dekoracyjne
  - `light.boneio_dr_8ch_03_2c7fbc_chr_04`: **Korytarz Lustro** (Lustro w korytarzu wejściowym)
- **Taras Dual Lamps (`light.taras_lampy`):**
  - Combines `light.bulb_1` (*Taras Lampa Lewa*) and `light.bulb_2` (*Taras Lampa Prawa*) into a unified light group toggled together from dashboard and voice.

### Security/Alarm Automations
- Alarm trigger on door/windows breach (`siren.reolink_duo_floodlight_poe_syrena`)
- Tag-based door access (RFID tags) with presence verification and auto-disarm
- Door/window reed sensor controls
- Electric strike lock (`lock.rygiel_drzwi_wejsciowych_lock`, `input_button.btn_entrance_door`)

### Roller Blind Automations
- Bedroom roller up/down control (`script.roller_bedroom_*`)
- Sunrise/sunset and morning wake-up scheduling

### Dynamic Energy Pricing (Pstryk)
- Hourly dynamic pricing via `config/pstryk_engine.py` (`pstryk_pricing.py` is legacy/unused)
- Sensors: `sensor.pstryk_price_meter_dol`, `_gora`, `sensor.pstryk_best_window_dol`, `_gora`
- Binary sensors: `binary_sensor.pstryk_in_best_window_dol`, `_gora`
- Automations:
  - Daily Pstryk price notification (22:00)
  - Tesla Charging Best Window automation (`switch.tesl_y_charge`)
  - Pstryk Best Window Voice Announcement (`pstryk_best_window_voice_announcement` on Voice PE via Nabu Casa TTS)

### Voice Assistant & Radio Playback
- **Hardware:** Home Assistant Voice PE (`media_player.home_assistant_voice_0a9bfd_media_player`)
- **Stateful Memory:** `input_select.last_radio_station` stores the active station (default: Eska Rock)
- **8 Supported Stations:** Eska Rock, RMF FM, Radio ZET, Antyradio, Radio 357, TOK FM, VOX FM, Polskie Radio Trójka
- **Scripts:** `script.play_radio`, `script.stop_radio`, `script.radio_volume_up`, `script.radio_volume_down`
- **Automations:** `morning_radio_schedule` (weekday Radio ZET, weekend Antyradio), `radio_station_changed_auto_play`

### Multimedia & TV Control (Salon TCL Google TV)
- **Primary Entities:** `media_player.salon_2`, `remote.salon_2` (Android TV Remote)
- **Cast Target:** `media_player.googletv8897_2` (Google Cast for camera streaming)
- **Streaming Apps:** Netflix, YouTube, Disney+, Max, Spotify, Prime Video via `script.tv_launch_app`
- **Camera Streaming (5 cameras):** Podwórze, Wejście, Taras, Ogród, Front via `script.tv_show_camera`
- **Cinema Mode:** `script.salon_cinema_mode` (activates `scene.salon_kino_1`, dims lighting after dusk)

## Docker Commands

```bash
# Start/stop Home Assistant
docker-compose up -d
docker-compose down

# View logs
docker logs -f homeassistant-homeassistant-1

# Shell access
docker exec -it homeassistant-homeassistant-1 sh

# Start Zigbee2MQTT
./docker-zigbee2mqtt.sh

# View Zigbee2MQTT logs
docker logs -f homeassistant-zigbee2mqtt-1

# === Automated AI Test Harness ===
python3 tests/runner.py --auto         # Auto git-diff impact test
python3 tests/runner.py --tier all     # Run complete 4-tier suite
python3 tests/runner.py --area salon   # Run targeted room tests
python3 tests/runner.py --discover     # Analyze coverage gaps
python3 tests/runner.py --generate <x> # Synthesize new test scenario
python3 tests/runner.py --stop         # Teardown test containers
```

**WARNING:** `clear_retained_simple.sh` contains hardcoded MQTT credentials (`MQTT_PASS`).

## Secrets

Secrets live in `config/secrets.yaml` and `config/.env` (the only env file; the only template is `config/.env.example`; no root-level copies) — never paste values into docs.

## Important Files

| File | Purpose |
|------|---------|
| `AI_DEVELOPMENT_GUIDELINES.md` | Mandatory AI development rules, planning gates, doc sync protocols |
| `llms.txt` | Standard LLM & human project index and architecture map |
| `PLACES_AND_BEHAVIORS.md` | Room-by-room entity directory & behavioral map |
| `AGENT.md` | Unified Home Assistant & ESP32/Node.js ecosystem bridge |
| `AGENTS.md` | Primary Codex developer instructions |
| `tests/runner.py` | 4-tier autonomous AI test harness CLI |
| `tests/test_registry.yaml` | Static baseline regression test catalog |
| `tests/docker-compose.test.yml` | Ephemeral test stack (HA + Mosquitto) |
| `docker-compose.yml` | Home Assistant container definition |
| `docker-zigbee2mqtt.sh` | Zigbee2MQTT start script |
| `config/mqtt.yaml` | All MQTT entity configurations |
| `config/automations.yaml` | 60+ automation rules |
| `config/scripts.yaml` | Operational scripts (radio, TV, blinds, lighting) |
| `config/pstryk_engine.py` | Pstryk dynamic energy & multi-period aggregation engine |
| `tests/unit/test_pstryk_engine.py` | Pstryk engine unit test suite (Tier 1B) |
| `config/secrets.yaml` | Sensitive configuration |
| `data/configuration.yaml` | Zigbee2MQTT settings |

## Security Notes

- **CRITICAL:** `clear_retained_simple.sh` has hardcoded MQTT credentials
- MQTT runs on plain port 1883 without TLS
- Home Assistant uses `network_mode: host` - direct network access
- Zigbee network key stored in plain configuration

## Related Documentation

- `AI_DEVELOPMENT_GUIDELINES.md` - Authoritative AI development guidelines & protocols
- `llms.txt` - Standard LLM & human ecosystem overview
- `PLACES_AND_BEHAVIORS.md` - Complete place/room behavioral map & entity directory
- `AGENT.md` - Unified bridge between Home Assistant & ESP32/Node.js edge
- `CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md` - Generic Camera AI & Face Recognition Engine proposal
- `VOICE_AI_IMPROVEMENTS_PROPOSAL.md` - Voice AI & entity remediation proposal
- `/Users/wtrzonkowski/Desktop/private/ARCHITECTURE.md` - Technical architecture
- `/Users/wtrzonkowski/Desktop/private/HOME_AUTOMATION_ECOSYSTEM.md` - System overview
- `/Users/wtrzonkowski/Desktop/private/ISSUES.md` - Known issues
- `/Users/wtrzonkowski/Desktop/private/home_automation/` - ESP32/Node.js backend
