# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

> **Key Companions:**
> - [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md) — Mandatory AI development rules, planning gates, and documentation update protocols.
> - [`llms.txt`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/llms.txt) — Global LLM & human project index and architecture map.
> - [`PLACES_AND_BEHAVIORS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/PLACES_AND_BEHAVIORS.md) — Exhaustive room-by-room entity directory and behavioral logic map.
> - [`AGENT.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENT.md) — Unified Home Assistant & ESP32/Node.js ecosystem bridge.

## Core AI Operating Rules

1. **Strict Planning Mode Gate (Read-Only Gate):** When in `/plan`, design mode, or preparing artifacts, AI agents must remain strictly read-only. NEVER modify, create, or delete workspace files without explicit conversational user approval. Automated stop-hook approvals do NOT grant permission to execute.
2. **Mandatory Documentation Self-Update:** Any modification to code, configuration, YAML, automations, scripts, entities, or hardware MUST trigger an automatic update to documentation (`PLACES_AND_BEHAVIORS.md`, `AGENT.md`, `AGENTS.md`, `llms.txt`) before completing the task.
3. **Git Commit & Push Safety:** NEVER run `git add`, `git commit`, or `git push` unless explicitly commanded using the exact words 'commit' or 'push'. Leave all changes unstaged.
4. **Context Rule:** Always start documentation and implementation tasks by reading [`AGENT.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENT.md) and [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md).
5. **Session Retrospective & Quality Re-Validation:** At the conclusion of every development session, conduct a structured retrospective re-validating domain logic, hardware flags (least privilege), continuous feedback, and test coverage before finalizing.
6. **Strict English Code & Identifiers Standard:** All variables, identifiers, function/class names, YAML keys, Jinja variables, entity IDs, unique IDs, and CLI arguments MUST be pure English. Never mix languages (no hybrid Ponglish like `jutro_date` or `_jutro_dol`). Polish is strictly reserved for user-facing UI labels, dashboard titles, notifications, and spoken TTS output.
7. **Single Source of Truth & Mandatory Variable Pre-Check Protocol (Rule 10):** All fallback defaults must live in exactly ONE place: the environment templates (`config/.env.example`). Never hardcode fallback literals (`16.13`, `51.2`, `100.0`, etc.) in Python or YAML. Before adding any new variable or helper, execute `python3 scripts/check_duplicate_variable.py --search <term>` to inspect existing variables and reuse them. Validate with `python3 scripts/check_duplicate_variable.py --validate`.
8. **Lovelace Dashboard Schema & Indentation Integrity (Rule 11):** In `type: conditional` cards wrapping other cards (e.g. `type: entities`), child card configuration keys (`entities:`, `cards:`) must be strictly indented under `card:`. Never place `entities:` or `cards:` as siblings of `card:` (which produces valid YAML but triggers runtime frontend failure "No entities specified"). All `type: entities` cards must define non-empty `entities:`. Invariant enforced by `tests/invariants/test_dashboard_schemas.py`.

## Project Overview

This is a Home Assistant deployment project using Docker for home automation control. It integrates with the `home_automation` project which contains ESP32 heating controllers and Node.js backend.

**Key Components:**
- **Home Assistant** - Main UI, automations, MQTT sensors/climate controls
- **Zigbee2MQTT** - Zigbee device integration (Sonoff Zigbee 3.0 USB Dongle Plus V2)
- **Pstryk Energy Pricing** - Dynamic energy pricing from Polish provider
- **Home Assistant Voice PE** - Voice satellite device (ESPHome, Nabu Casa) for voice assistant and TTS announcements

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
├── scripts.yaml          # HA scripts (roller, lighting, cinema)
├── scenes.yaml           # Scene definitions
├── templates.yaml        # Template sensors (Pstryk pricing)
├── rest.yaml             # REST sensors (InPost Paczkomat MAR13M air quality & weather)
├── customize.yaml        # Entity customization
├── secrets.yaml          # Sensitive configuration
├── views.yaml            # View definitions
├── dashboard_modern_reference.yaml # Dom iPad Modern dashboard (/dashboard-home)
├── dashboard_home_improved.yaml   # Home dashboard
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
├── pstryk_engine.py      # Dynamic energy & metering engine (caching, aggregations, unit-tested)
├── energy_defaults.py    # Single source of truth for energy defaults (Rule 10, AST validated)
└── solar_engine.py       # Dynamic solar & astronomical calculation engine (Astral 2.2, 3-tier fallback)

scripts/
└── check_duplicate_variable.py # Mandatory pre-check search & AST validation tool (Rule 10)

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

## Automations Overview

### Heating/Routine Automations (18)
- Thermostat routine changes based on input_select for local01-local06

### Lighting Automations & Architecture
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
- **Salon Ceiling Lighting Helpers:**
  - `input_number.kolor_bialy_salon` (White channel level 0–255) and `input_number.kolor_czerwony_salon` (Red channel level 0–255) for MQTT ceiling light and accent control.

### Security/Alarm Automations
- Alarm trigger on door/windows breach
- Tag-based door access (RFID tags)
- Door/window reed sensor controls

### Roller Blind Automations
- Bedroom roller up/down control
- Sunrise/sunset scheduling

### Energy/Pricing
- Daily Pstryk price notification (20:00)
- Tesla Charging Best Window automation
- Pstryk Best Window Voice Announcement (`pstryk_best_window_voice_announcement` - plays dynamic TTS announcement on Home Assistant Voice PE `media_player.home_assistant_voice_0a9bfd_media_player` via Nabu Casa Cloud TTS)

### Voice Assistant & Radio Playback
- **Hardware:** Home Assistant Voice PE (`media_player.home_assistant_voice_0a9bfd_media_player`)
- **Voice Intents:**
  - `WlaczRadio`, `ZatrzymajRadio` (custom sentences in `config/custom_sentences/pl/dom_sentences.yaml` and `config/custom_sentences/en/dom_sentences.yaml`; supports starting and switching stations: *"włącz radio [stacja]"*, *"włącz [stacja]"*, *"zmień stację na [stacja]"*, *"przełącz na [stacja]"*, *"switch radio to [station]"*)
  - `GlosniejRadio`, `CiszejRadio` (*"głośniej radio"*, *"podgłośnij radio"*, *"ciszej radio"*, *"ścisz radio"* / *"volume up radio"*, *"radio volume down"*)
- **Stateful Memory:** `input_select.last_radio_station` stores the last active station (resumed when saying *"włącz radio"* / *"play radio"* without specifying a station; defaults to Eska Rock on initial run)
- **Scripts:**
  - `script.play_radio`: resolves stream URL via multi-alias dictionary (handling canonical IDs like `rmf_fm` as well as natural spoken aliases like `"RMF FM"`, `"Radio ZET"`, `"357"`), updates helper only when changed, streams directly on Voice PE via `media_player.play_media` (ESPHome Voice PE does not support `media_player.turn_on`)
  - `script.stop_radio`: stops Voice PE stream cleanly via `media_player.media_stop` (ESPHome does not support `media_player.turn_off`); cleanly halts running `morning_radio_schedule` routine if active
  - `script.play_morning_music`: randomly picks and plays an audio file from dedicated folder `/config/media/morning_music/` on Voice PE
  - `script.toggle_radio`: smart toggle checking if Voice PE is playing -> `script.stop_radio`, otherwise -> `script.play_radio`
  - `script.radio_volume_up`: increases volume on Voice PE via `media_player.volume_up`
  - `script.radio_volume_down`: decreases volume on Voice PE via `media_player.volume_down`
- **Supported Stations:** Eska Rock (default), RMF FM, Radio ZET, Antyradio, Radio 357, TOK FM, VOX FM, Polskie Radio Trójka
- **Automations:**
  - `morning_radio_schedule`: Two-step wake-up routine: Step 1 plays random MP3 track from `/config/media/morning_music/` (~3 min) at 40% volume via `script.play_morning_music`, Step 2 starts scheduled radio at 10% volume (Mon-Fri at `input_datetime.pora_pobudka` default 07:00 -> Radio ZET; Sat-Sun at `input_datetime.pora_pobudka_weekend` default 08:15 -> Antyradio on Voice PE)
  - `radio_station_changed_auto_play`: Automatically switches radio stream when user selects a different station in `input_select.last_radio_station` while radio is playing; guarded by `not is_state('script.play_radio', 'on')` to prevent re-entrant cancellation
  - `voice_pe_media_playback_intercept`: Intercepts HA `call_service` events (`media_play`, `media_play_pause`, `media_pause`) targeting Voice PE and maps them to `script.play_radio`, `script.toggle_radio`, and `script.stop_radio`

### Multimedia & TV Control (Salon TCL Google TV)
- **Primary Entities:** `media_player.salon_2`, `remote.salon_2` (Android TV Remote integration)
- **Cast Target:** `media_player.googletv8897_2` (Google Cast integration for camera streaming)
- **Streaming Apps:** Netflix (`com.netflix.ninja`), YouTube (`https://www.youtube.com`), Disney+ (`com.disney.disneyplus`), Max (`https://play.max.com`), Spotify (`spotify://`), Prime Video (`https://app.primevideo.com`)
- **Camera Streaming (5 cameras):**
  - Podwórze: `camera.reolink_duo_floodlight_poe_plynny_2`
  - Wejście: `camera.rlc_822a_plynny`
  - Taras: `camera.taras_plynny`
  - Ogród: `camera.rlc_820a_niska_rozdzielczosc`
  - Front / Brama: `camera.520a_plynny`
- **Scripts:**
  - `script.tv_launch_app`: Launches streaming apps by key on `media_player.salon_2` via `remote.turn_on` and `media_player.play_media`
  - `script.tv_show_camera`: Streams camera via `camera.play_stream` (format: HLS) to `media_player.googletv8897_2`, or stops stream (`camera: stop`)
  - `script.salon_cinema_mode`: Turns on TV and activates `scene.salon_kino_1`. If after dark (`after: input_datetime.dynamic_sunset, before: input_datetime.dynamic_dawn`), sets `input_select.salon_kolor_wybor` to option 2 (`script.light_salon_color_2`), executes it, and turns off ceiling light `light.salon_mqtt`
- **Dashboard View:** `/dashboard-home/media` in `config/dashboard_modern_reference.yaml` (Dom iPad Modern) and `config/dashboard_home_improved.yaml` featuring side-by-side TV controls (tile with volume slider, Cinema Mode toggle, power toggle) and Radio Voice PE component (tile with volume slider and toggle tap action, 2-column Włącz / Wyłącz action grid, reactive station dropdown selector), 6-app quick launch grid, scenes & mood grid, conditional D-pad remote, and 5 camera glance cards with Cast / Stop buttons
- **Voice Intents:**
  - `WlaczAplikacjeTV`: *"włącz [aplikacja] na telewizorze"* / *"open [app] on tv"*
  - `GlosniejTV`, `CiszejTV`, `WyciszTV`: *"głośniej / ciszej / wycisz telewizor"* / *"volume up / down / mute tv"*
  - `PokazKamereTV`, `ZatrzymajKamereTV`: *"pokaż [kamera] na telewizorze"* / *"show [camera] on tv"*, *"zamknij podgląd kamery"*
  - `TrybKinoSalon`, `ZatrzymajKinoSalon`: *"tryb kino w salonie"* / *"cinema mode in living room"*, *"wyłącz tryb kino"*

### Image Recognition, Face Recognition & License Plate Recognition (ALPR)
- Camera snapshot with Google Generative AI (Gemini Vision) analysis
- **Proposal & Roadmap:** [`CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md`](CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md) — Generic Camera AI Vision Engine (`script.camera_ai_analyze`) with biometric face recognition (door strike unlocking) and Automatic License Plate Recognition (ALPR/ANPR for sliding gate access and driveway illumination)

## Docker Commands

```bash
# Start/stop Home Assistant
docker-compose up -d
docker-compose down

# Restart Home Assistant
sudo docker restart homeassistant

# View logs
docker logs -f homeassistant

# Shell access
docker exec -it homeassistant sh

# Start Zigbee2MQTT
./docker-zigbee2mqtt.sh

# View Zigbee2MQTT logs
docker logs -f homeassistant-zigbee2mqtt-1
```

**WARNING:** `clear_retained_simple.sh` contains hardcoded MQTT credentials (MQTT_PASS=<redacted>).

## Key Secrets (secrets.yaml)

```yaml
latitude_home: 52.34453253634735
longitude_home: 21.099288761615757
Camera_URL: "http://10.20.2.7/cgi-bin/api.cgi?..."
CameraFront_Source: "rtsp://homeAssistant:<redacted>@10.20.2.7:554/..."
# Two separate Pstryk installations:
pstryk_api_key_dol: "<redacted: see config/secrets.yaml>"    # dół
pstryk_api_key_gora: "<redacted: see config/secrets.yaml>"  # góra
```

## Energy Pricing, Multi-Window Dispatch & Battery Storage System (Pstryk & Deye)

**Scripts & Drivers:**
- `config/energy_defaults.py`: Strict Single Source of Truth architecture (Rule 10) dynamically loading baseline templates (`config/.env.example`) and env overrides (`config/.env`), eliminating duplicate hardcoded literals (`16.13`, `51.2`, `100.0`) from Python/YAML.
- `scripts/check_duplicate_variable.py`: Mandatory pre-check search & AST validation tool (`--search <term>`, `--validate`) preventing redundant entity creation and enforcing single-source reuse.
- `config/pstryk_engine.py`: Multi-period backend aggregation & dispatch engine (latest, forward 48h, today hourly, month daily, year monthly).
- `config/pstryk_pricing.py`: legacy CLI wrapper (unused; `pstryk_engine.py` is the engine).
- `config/solarman_v5_client.py`: Zero-dependency Deye Modbus-RTU over Solarman V5 TCP socket client with auto-discovery (`--scan`), dynamic IP persistence, and connection refusal fast-fail (immediate break on `ConnectionRefusedError` to eliminate redundant socket retry delays and transition straight to Cloud OpenAPI fallback).
- `config/deye_cloud_client.py`: Zero-dependency Deye Cloud OpenAPI client (`https://eu1-developer.deyecloud.com`) aligned with official POST endpoints (`device/latest`, `config/battery`, `config/tou`, `order/sys/tou/update`, `order/battery/parameter/update`, `order/customControl`) and OAuth token endpoint (`POST /v1.0/account/token?appId={AppId}`). Automates 60-day token acquisition using App ID/Secret and Account credentials (`DEYE_CLOUD_EMAIL`, `DEYE_CLOUD_PASSWORD`) with in-memory SHA-256 hashing. Provides live telemetry ingestion (3-phase grid power and voltages, house consumption, daily counters, AC temperature, battery SOC/voltage) with resilient pre-battery / no-PV state support. TOU schedule updates (`order/sys/tou/update`) format 6 time slots (`HH:mm`), enforce integer voltage, disable backup generator charging (`enableGeneration: false`), enforce zero-export self-consumption (`enableSell: false`), and handle `1106000` order receipt. Battery parameters (`order/battery/parameter/update`) support `paramterType`: `BATT_LOW` (Min SOC) and `MAX_CHARGE_CURRENT`, while `order/customControl` provides raw Modbus Function 0x06 dispatch (Shutdown SOC). CLI support includes `--get-token`, `--read-all`, `--read-soc`, `--sync-pstryk-tou`, `--set-safety-limits`, and `--set-charge-current`.
- `config/.env.example` (the ONLY template; the real `config/.env` is the only env file, no root copies): Configuration templates defining Deye network IPs, serial numbers (`DEYE_LOGGER_SN`, `DEYE_INVERTER_SN`), Developer App credentials (`DEYE_CLOUD_APP_ID`, `DEYE_CLOUD_APP_SECRET`), Account credentials (`DEYE_CLOUD_EMAIL`, `DEYE_CLOUD_PASSWORD`), and Pstryk API keys (`PSTRYK_API_KEY_DOL`, `PSTRYK_API_KEY_GORA`). All engines read secrets directly from `.env` (or `config/.env`) with secondary fallback to `secrets.yaml`. Real `.env` files are git-ignored.
- **Generic UI Pending Operations & Dashboard Blocking Architecture:**
  - `input_text.pending_ui_operations`: Single central helper storing JSON array of strings representing active in-flight UI operations (e.g. `["deye_battery_safety"]`, default `[]`).
  - Management scripts: `script.set_ui_operation_pending(operation: string)` (appends operation to array), `script.clear_ui_operation_pending(operation: string)` (removes operation), and `script.clear_all_pending_ui_operations` (emergency reset).
  - Sensors: `binary_sensor.ui_any_operation_pending`, `binary_sensor.ui_op_pending_deye_battery_safety`, `binary_sensor.ui_op_pending_deye_tou_sync`.
  - Auto-Clear Safety: Automation `auto_clear_stale_pending_ui_operations` automatically clears pending UI operations after 90 seconds if an operation hangs or crashes.
  - Lovelace Dashboard Integration: Dashboards (`dashboard_modern_reference.yaml`, `dashboard_home_improved.yaml`) render conditional in-progress banners (`⏳ Trwa Zapisywanie...`), hide action buttons, and lock parameter sliders when `ui_op_pending_deye_battery_safety` is `on`.
  - Latency Optimization: `sync_deye_inverter_tou` settle delay removed, duplicate `homeassistant.update_entity` call removed from `confirm_and_apply_deye_battery_settings`, reducing UI roundtrip from ~35s to ~7-10s.
**Hardware:** Deye 12 kW Hybrid Inverter (`SUN-12K-SG05LP3-EU-SM2`, SN `2603160727`, Station `Parkowa13a`) at dynamic IP (`input_text.deye_inverter_ip`, default `10.20.2.6`), port `8899` (`input_number.deye_inverter_port`, configured with numeric text input `mode: box`) + LiFePO4 battery pack (dynamic capacity, 16S cells topology, Bluetooth BMS) with pre-battery mode toggle (`input_boolean.deye_battery_installed`).
**API:** Polish energy provider unified-metrics API (`https://api.pstryk.pl/integrations/meter-data/unified-metrics/`) & Deye Cloud OpenAPI (`https://eu1-developer.deyecloud.com`).
**Caching:** Smart atomic file cache in `/tmp/pstryk_cache_{installation}.json` with 15-minute TTL for live metrics and persistent historical frames. All Deye reads implement an intelligent 30s TTL write-on-change persistent cache in `/config/.deye_inverter_cache.json` (fallback `/tmp/.deye_inverter_cache.json`) with semantic diffing to protect flash storage and resilient offline fallback.
**Dynamic Network Discovery & Dual-Tier Fallback:** Subnet scanner (`solarman_v5_client.py --scan`) probing port 8899 and verifying Solarman V5 frame response (`0xA5..0x15`). If local port 8899 is closed or refused, system seamlessly falls back to `deye_cloud_client.py`. Persists discovered IP in `/config/.deye_inverter_config.json`, triggered from UI via `input_button.scan_deye_inverter_ip` and script `script.scan_and_set_deye_inverter_ip`.
**Consolidated Schema:** Retains top-level `current_price`, `start`, `end`, `duration_hours`, `all_prices`, `net`, `gross`, `battery_model`, `ev_best_window`, `powerbank_best_window`, `best_sell_window`, `deye_tou_schedule`, `plan_timeline` and `horizon_end`.
**Two installations:** `dol` (lower/ground floor) and `gora` (upper floor)

**Hardware Invariants & Control Policy:**
**Dynamic Register 108 Modulation:** Modbus Holding Register 108 (`REG_MAX_CHARGE_CURRENT`) is set by the engine: inside a grid-charge slot to that slot's planned current (see Greedy Cheapest-Hour Charging), outside every charge slot to the 100A ceiling (Rule 12, no pre-set: a reduced-current charge may run up to one sync interval at the ceiling before the sync inside it modulates Reg 108). Writes are strictly idempotent.
**Engine/HA contract (current):** Reg 108 (max charge current) IS written by the engine (modulation via `set_charge_current`/`--restore-charge-current`), always based on `BATTERY_CHARGE_CURRENT_MAX_CEILING_A` (100A), never on the live register value (no ratchet); `--restore-charge-current` restores 100A. Reg 109 is never written. Reg 112 (Max SOC) is never written: `input_number.deye_battery_max_charge_soc` is the source of truth and is no longer overwritten from the inverter. Min/Shutdown SOC (Reg 110/111) are written only via the Confirm button. `input_boolean.deye_battery_installed` persists across restarts (no `initial:`); battery automations/templates run when it is `on` OR telemetry reports a battery installed; toggle is on both dashboards. `input_select.battery_discharge_target_hours` / `ev_charging_target_hours` drive the engine via `--sell-target-hours` / `--ev-target-hours` (EV `auto` = 2h weekday / 4h weekend, weekday computed in Europe/Warsaw; sell default 3h); cached payloads are reused only if target hours match. Helpers (selects, inverter IP/port) no longer reset on restart (`initial:` removed). `script.sync_deye_inverter_tou`: `mode: queued`, `max: 2`, `continue_on_error`; on success sets `input_datetime.deye_tou_last_synced_date` and refreshes `sensor.deye_tou_last_sync`, `sensor.deye_inverter_status`, `sensor.pstryk_price_meter_dol` and `_gora`; on failure the date is not touched. TOU generation is minute-based, midnight-wrap safe, always 6 strictly increasing slots (slots follow the price valley/peak timeline; standby holds min SOC); the plan runs in Europe/Warsaw time; TOU is not written (and the payload not cached) if the day's prices are incomplete. Solarman TOU write aborts on the first failed register and then falls back to Deye Cloud (at most once); cloud slots carry the nominal voltage. Unit tests are hermetic (sockets blocked, tmp paths); Tier 2 sandbox must never contain the real `.env` or call real engine/client scripts. `config/pstryk_pricing.py` is legacy/unused; `pstryk_engine.py` is the engine.
- **Hardware Safety Registers (109..112):** Registers 109 and 112 are never written. Critical safety registers 110 (Min SOC) and 111 (Shutdown SOC) are written **exclusively** upon explicit 2-step user confirmation via `apply_confirmed_battery_safety_limits` (with immediate cache write-through and Deye Cloud OpenAPI fallback). Register 109 (Max Discharge Current, 140A) and 112 are never written (Max SOC lives in the slider).
- Home Assistant ingests all parameters in **Read-Only** mode via `sensor.deye_inverter_status` (`solarman_v5_client.py --read-all` or `deye_cloud_client.py --read-all`).
- **Time-of-Use (TOU) Programming:** Permitted write operations are the 6-slot Time-of-Use schedule (registers `248..273`), dynamic Register 108 charging modulation (`set_charge_current`), and confirmed safety limits (registers 110 & 111 via `apply_confirmed_battery_safety_limits`).
- **Rule 9 Zero-Hardcoding Invariant:** Battery capacity, nominal voltage, and maximum currents must NEVER be hardcoded as static numbers anywhere in configuration or templates. See [`SPEC_DEYE_BATTERY_SYSTEM.md`](SPEC_DEYE_BATTERY_SYSTEM.md).
- **Rule 10 Single Source of Truth & Pre-Check Invariant:** Fallback values (including 140.0A max discharge current) must exist in exactly one place (`config/.env.example`). Python and YAML must never hardcode raw fallback literals. `--validate` also flags any `.get(key, <non-zero numeric literal>)` in the engine and client modules (fallbacks must come from `energy_defaults.py`). All additions must be pre-checked via `python3 scripts/check_duplicate_variable.py --search <term>` and validated via `--validate`.

**Operating Bounds & Reserves:**
- Battery Total Capacity: strictly read-only hardware specification via `sensor.powerbank_total_capacity_kwh` dynamically computed from Deye Holding Register `102` (`batt_capacity` in Ah) and nominal voltage, or Cloud OpenAPI (`battCapacity`). Static fallbacks are strictly prohibited.
- Nominal Battery Voltage: strictly read-only via `sensor.powerbank_voltage_v` resolved dynamically from live telemetry (Register 587) or Cloud OpenAPI.
- Max Charge Current: dynamically read from Deye Holding Register `108` and modulated during extended charging windows.
- Max Discharge Current: strictly read-only via `sensor.deye_inverter_max_discharge_current` dynamically read from Deye Holding Register `109` (140A).
- Active Editable Sliders: `input_number.deye_battery_max_charge_soc` (50–100%, default 90%), `input_number.deye_battery_min_discharge_soc` (10–50%, default 20%), `input_number.deye_battery_shutdown_soc` (0–30%, default 5%).
- 2-Step Confirmation Process: modifications to critical safety sliders (`min_discharge_soc`, `shutdown_soc`) set `binary_sensor.deye_battery_critical_parameters_pending` to `on`, pausing auto-sync. User must confirm via "Zatwierdź i Zastosuj" (`script.confirm_and_apply_deye_battery_settings` with confirmation dialog) or cancel via "Cofnij do Falownika" (`script.revert_deye_battery_settings_from_inverter`).
- Operating usable capacity: $((MaxSOC - MinSOC) / 100) \times Capacity$ (dynamic, reports 0.0 kWh when battery uninstalled).
- Blackout reserve: $((MinSOC - ShutdownSOC) / 100) \times Capacity$ (dynamic, reports 0.0 kWh when battery uninstalled).
- Top cell-protection buffer: $((100\% - MaxSOC) / 100) \times Capacity$ (dynamic).
- Base discharge power: $P_{\text{base}} = \min(12000, \max(1000, \operatorname{round}(I_{\text{discharge}} \times V_{\text{nom}}, -1)))$ (e.g. 140A @ 51.2V = **7.17 kW** across all 6 slots).

**Core Dispatch Windows & TOU Schedule:**
- **EV Window:** Weekdays 2h continuous / Weekends 4h continuous in `auto` (selectable via `input_select.ev_charging_target_hours`) (`sensor.pstryk_ev_best_window_dol`, e.g. `01:00 - 03:00 (śr. 0.88 zł/kWh)`).
- **Power Bank Window:** the cheapest block of N hours inside a price valley, N = max(`CHARGE_WINDOW_MIN_HOURS`, energy need at the Reg 108 ceiling); informational only (sensors, 22:00 notification, explicit-window path), it does not drive the greedy grid charge plan, 1-hour granularity (e.g. `11:00 - 14:00 (śr. 0.90 zł/kWh)`).
- **TOU Power Semantics:** In Deye hybrid inverters `Power(W)` in TOU registers 256–261 is the maximum battery discharge power to load/grid, not the charge speed. All 6 slots keep full base discharge power ($P_{\text{base}} \approx 7170\text{ W}$) so household load coverage is never throttled.
**Greedy Cheapest-Hour Charging (`plan_charge_hours`):** Worth-covering hours are priced above the cheapest candidate × (1 + loss%). Candidate hours (now → last published price) are taken in ascending price order; each is charged at the most energy it can store (Reg 108 ceiling × V × (1 − loss%), capped by room to Max SOC), and only if it covers a projected shortfall in a later worth-covering hour that is dearer than it × (1 + loss%). The loop stops when no coverable shortfall remains. An hour must store and serve at least one `TOU_FLOOR_SOC_STEP` (no trickle). Load is the real Deye profile. Example (two-day fixture, real weekday profile): SOC 17 at 09:27 → charges 13:00, 14:00, 23:00 and next-day 13:00 at 100 A; SOC 60 → 13:00 and next-day 13:00.
**Rolling TOU Plan (`plan_rolling_tou`):** One rolling plan from *now* to the last published price hour (up to ~48 h), returned as a merged `plan_timeline` (`{start, end, role, target_soc, soc_start, soc_end, charge_a, price}`, roles `charge`/`peak`/`hold`/`drain`). Peaks are classified per price day (each Europe/Warsaw day's own mean gross price). Grid charge hours come from `plan_charge_hours` (fill-to-top instead during calibration or without a load profile); the planner receives the inverter's active slots (`active_slots`). Explicit `charge_windows` (legacy explicit-window path) bypass the greedy planner. The dashboard shows this timeline as "Plan Baterii".
- **Hardware Constraint (6 Clock-Keyed Slots):** Deye TOU is 6 slots repeating daily, so `deye_tou_schedule` is the 24 h starting from the behaviour running now. The hourly re-sync writes this rolling window (only when it changes, `are_tou_slots_identical`); beyond the last published price the table repeats today's pattern. The sync also runs when tomorrow's prices publish.
- **Load Forecast from Real Usage (`fetch_consumption_history`, `build_load_profile`, `load_forecast`):** `deye_cloud_client.fetch_consumption_history` is read-only (`POST /v1.0/station/list`, `POST /v1.0/station/history`, granularity 1, 5-min frames, `startAt`/`endAt` with `startDate`/`endDate` fallback) over the last `LOAD_HISTORY_DAYS` (.env, 14) full Warsaw days ending yesterday. Cache `/config/.deye_load_history.json` (override `DEYE_LOAD_HISTORY_FILE`), written only on change, refreshed at most once per Warsaw day. `build_load_profile` gives weekday and weekend 24 h kWh/h profiles (Warsaw local hours, DST-correct); `load_forecast` gives the per-hour forecast (running hour blended with live load, snapped to `LOAD_ESTIMATE_STEP_KW`). CLI `deye_cloud_client.py --fetch-history` (read-only). No history means no load forecast: the plan then fills to Max in the cheapest hours (see Floors).
- **History job & negative cache:** History is fetched ONLY by its own daily job (automation `deye_load_history_daily_fetch` at 04:10 and at HA start -> `shell_command.fetch_deye_load_history` -> `deye_cloud_client.py --fetch-history`); the engine (`load_household_profile`, `rolling_plan_fields`, `fetch_consolidated_data`) reads ONLY the cache file `/config/.deye_load_history.json` and never calls Deye Cloud. Negative cache: a failed fetch writes `{last_attempt, failed: true}` (old frames kept) and retries happen at most every `DEYE_HISTORY_RETRY_HOURS`; the working request variant (`startAt/endAt` vs `startDate/endDate`) is remembered in the cache; days are fetched newest first and the walk stops at the first day failing both variants; `fetched_date` is always stored so same-day reuse works. Status in `input_text.deye_load_history_status` (ok/error). New env var (`config/.env.example` only): `DEYE_HISTORY_RETRY_HOURS`. Stale-sync alert: automation `deye_tou_stale_sync_alert` raises a persistent_notification when `input_datetime.deye_tou_last_synced_date` is older than `input_number.deye_tou_stale_sync_hours` (0 = off; set 36 in the UI, no `initial:`); expected behaviour: when sync stops, the inverter replays the last TOU table daily.
**Flash-Wear Hysteresis:** `are_tou_slots_identical()` compares behaviour at every minute of the daily circle (grid charge exact; other floors ±`TOU_FLOOR_SOC_STEP`; power ±`TOU_POWER_DIFF_TOLERANCE_W`), so a boundary that only splits one behaviour does not force a write. The planner gets the inverter's active slots: a grid-charge hour already on the inverter is held while it stores or serves any shortfall; the table anchor is the running inverter slot; the tail beyond the published prices is copied from the inverter's own slots. In tier-5 charge merges the higher target and current are kept.
**Dynamic 6-Slot Table & Idempotent Diff Check:** Always exactly 6 slots, minute-based and strictly increasing in [00:00, 24:00). `are_tou_slots_identical()` compares the new table with the inverter's active one (see Flash-Wear Hysteresis); identical tables are not written (`unchanged: true`).
**Floors (one SOC walk):** charge hours → grid charge to Max SOC; worth-covering hours → Min SOC (the battery serves the load); other hours → hold at the projected SOC, snapped down to `TOU_FLOOR_SOC_STEP`. Without a load profile or during BMS calibration the plan fills to Max (100 % in calibration) in the cheapest hours, with no trickle within one step of the top. Explicit windows keep the segment floors of `_assign_cell_floors`.
**Reg 108 Modulation Schedule (`check_and_apply_charge_current_modulation`):** The base is always the 100A ceiling (`min(100, env)`), never the live register value. Inside a grid-charge slot: that slot's planned current (`charge_a`), clamped to [`BATTERY_CHARGE_CURRENT_MIN_FLOOR_A`, ceiling]; outside every charge slot: the 100A baseline (Rule 12, no pre-set). `--restore-charge-current` restores 100A. Writes are idempotent. Regs 109 and 112 are never written; Regs 110/111 only via the Confirm button.
- **Price Coverage Gate:** TOU is neither written nor cached unless the Europe/Warsaw day is covered (`warsaw_day_covered()`: first frame at/before 00:00, last frame ends at/after the next 00:00, at most 1h (`PRICE_COVERAGE_MAX_MISSING_HOURS`) missing). The cache stores `_warsaw_date` and the target hours and is reused only when both match.
- **Inverter I/O Budget & Fallback:** The local Modbus budget (`LOCAL_TIME_BUDGET_S`, 25 s) starts at the first failed socket attempt; if active, local TOU writes are skipped and the Deye Cloud is used (at most once, `cloud_attempted`). `sync_tou_schedule()` aborts on the first failed register write. Cloud slots carry the nominal voltage.
- **TOU Read-Back Labels (`tou_slot_label`):** Relative to min/max SOC (±`TOU_LABEL_SOC_TOLERANCE`): grid charge = `Najtańsze Ładowanie`; at/above Max = `Czuwanie Przed Szczytem (Hold)`; at/below Min = `Szczyt Cenowy (Autokonsumpcja)`; between = `Rezerwa Przed Szczytem`.
- **CLI:** `pstryk_engine.py --print-tou` prints the rolling timeline and the 6-slot table (24 h from now) without writing.

**Sensors & Telemetry:**
- `sensor.deye_inverter_status`: Modbus reader returning `config`, `telemetry`, `active_tou`
- `sensor.deye_inverter_max_charge_soc`, `min_discharge_soc`, `shutdown_soc`: Read-only hardware parameters
- `sensor.deye_inverter_max_charge_current`, `max_discharge_current`: Read-only current limits
- `sensor.deye_battery_soc`, `voltage`, `power`, `current`: Live battery telemetry (registers 587..591)
- `sensor.deye_tou_active_schedule`: Active 6-slot schedule state and attributes
- `sensor.pstryk_price_meter_dol` & `sensor.pstryk_price_meter_gora`: Root command_line entities (PLN/kWh, 15-min update)
- `sensor.pstryk_best_window_dol` & `sensor.pstryk_best_window_gora`: Cheapest charging window range
- `sensor.pstryk_ev_best_window_dol` & `_avg_price_dol`: EV window with hours and average buy price (Today)
- `sensor.pstryk_powerbank_best_window_dol` & `_avg_price_dol`: Battery charge window with hours and average buy price (Today)
- `sensor.pstryk_best_sell_window_dol`, `_avg_price_dol`, & `sensor.pstryk_peak_sell_spike_dol`: Peak sell window and spike (Today)
- `sensor.pstryk_ev_best_window_tomorrow_dol`: EV window for Tomorrow (published ~12:00 Warsaw time)
- `sensor.pstryk_powerbank_best_window_tomorrow_dol`: Battery charge window for Tomorrow (`Oczekiwanie na publikację (ok. 12:00)` before publishing)
- `sensor.pstryk_best_sell_window_tomorrow_dol`: Peak sell window and spike for Tomorrow
- `sensor.deye_battery_operating_capacity_kwh` & `sensor.deye_battery_blackout_reserve_kwh`: Usable capacity & emergency reserve (dynamically calculated from registered capacity)
- `sensor.deye_charge_power_kw` & `sensor.deye_discharge_power_kw`: Transfer power limits (dynamically calculated from live voltage and current limits)
- `sensor.deye_battery_display_level`: Dual % and kWh readout (e.g. `85% (13.7 kWh)` or `"Oczekuje na montaż"` when uninstalled)
- `sensor.deye_battery_working_state`: Tri-state operational mode (`Ładowanie`, `Rozładowanie (Sprzedaż)`, `Czuwanie`)
- `sensor.deye_battery_days_since_calibration`: Days counter since last 100% BMS calibration
- `sensor.pstryk_cena_kupno_dol` & `sensor.pstryk_cena_kupno_gora`: Current gross buy rate (PLN/kWh) with pricing components
- `sensor.pstryk_cena_sprzedaz_dol` & `sensor.pstryk_cena_sprzedaz_gora`: Prosumer gross sell rate (PLN/kWh) with net selling price
- `sensor.pstryk_zuzycie_dzis_dol` & `sensor.pstryk_zuzycie_dzis_gora`: Today's consumed energy (kWh) with hourly breakdown history
- `sensor.pstryk_koszt_dzis_dol` & `sensor.pstryk_koszt_dzis_gora`: Today's gross electricity expense (PLN) with balance and revenue
- `sensor.pstryk_zuzycie_miesiac_dol` & `sensor.pstryk_zuzycie_miesiac_gora`: Month-to-date energy consumption (kWh) with daily history
- `sensor.pstryk_koszt_miesiac_dol` & `sensor.pstryk_koszt_miesiac_gora`: Month-to-date electricity expense (PLN) with monthly breakdown
- `sensor.pstryk_slad_weglowy_dol` & `sensor.pstryk_slad_weglowy_gora`: Live and today's carbon footprint (g CO₂)
- `binary_sensor.pstryk_in_best_window_dol` & `binary_sensor.pstryk_in_best_window_gora`: Active cheapest window flag
- `binary_sensor.pstryk_in_ev_best_window_dol`: Active EV charging window flag
- `binary_sensor.pstryk_in_powerbank_best_window_dol`: Active Power Bank charging window flag
- `binary_sensor.pstryk_in_sell_window_dol`: Active peak selling window flag
- `binary_sensor.pstryk_has_tomorrow_pricing_dol`: Flag indicating whether tomorrow's Day-Ahead prices are published (typically from ~12:00)
- `binary_sensor.pstryk_tania_godzina_dol` & `binary_sensor.pstryk_tania_godzina_gora`: Active cheap hour flag (`is_cheap`)
- `binary_sensor.pstryk_droga_godzina_dol` & `binary_sensor.pstryk_droga_godzina_gora`: Active expensive hour flag (`is_expensive`)

**Lovelace Dashboards:**
- **Overview View (`/dashboard-home/dom`):** `Energia i Info` box updated with Koszt Dziś (Dół & Góra), Okno EV (aktywne - `binary_sensor.pstryk_in_ev_best_window_dol`), kondycjonalnie Magazyn SunDeposit poziom (`%` i `kWh`) oraz stan tri-state (`Rozładowanie (Pokrycie Szczytu)`), Najtańsze Okno Magazynu (`sensor.pstryk_powerbank_best_window_dol`), Szczyt Cenowy (Zasilanie Bateryjne - `sensor.pstryk_best_sell_window_dol`), oraz Odpady.
- **Energy View (`/dashboard-home/energia`):** Stacked views featuring 4 dedicated sections: 1. Harmonogram Dyspozytorski Pstryk (EV, Magazyn, Szczyt Cenowy z zasilaniem z baterii), 2. Magazyn Energii & Falownik Deye 12 kW (telemetria, pojemność użyteczna, rezerwa blackout, moc), 3. Parametry Odczytane z Falownika Deye (Modbus Read-Only) oraz Harmonogram Time-of-Use — Dziś (TOU - Zapis o 00:30 + Retry z `input_datetime.deye_tou_last_synced_date`, natywna tabela GFM Markdown o zoptymalizowanych nagłówkach uwalniających 60% szerokości na tryb pracy, wyróżnionym ładowaniu `**⚡ TAK**`, czytelnymi podziałami sekcji `---`, blokami callout `>` z akcentem pionowym, łamaniem linii na etykiety i wartości w kodzie dla okien Dziś, oraz wyodrębnioną sekcją callout *🔮 Prognoza Dyspozytorska na Jutro* z oknami EV, Magazynu i Szczytu po publikacji ok. 12:00), 4. Zdrowie Baterii i Kalibracja BMS 100% z polem tylko do odczytu `Trwa Procedura Kalibracji 100%` (`binary_sensor.deye_battery_calibration_active`, tap_action: none) sterowanym przyciskiem ręcznym `input_button.trigger_deye_battery_calibration`, obok 16 kafelków instalacji Dół/Góra (zoptymalizowane `type: tile` z `command_timeout: 45`).
- **Reporting Subview (`/dashboard-home/energia-raport`):** Subview with native back navigation, side-by-side Dół vs Góra consumption & cost comparison, Jinja2 markdown tables for hourly today breakdown, daily month history, 2026 year monthly table, and 48h live history graphs.

**Automations & Scripts:**
- `deye_tou_hourly_recalculation` - Hourly recalculation triggered at minute 2 of every hour and 00:00:30 (guarded by `input_boolean.deye_battery_installed`). Recomputes 6-slot TOU schedule based on latest spot prices and battery SOC, uploading to Deye falownik only when schedule parameters have changed (`are_tou_slots_identical()` idempotent diff check).
- `deye_tou_daily_sync_with_retry` - Recalculates and uploads 6-slot TOU schedule once daily at `00:30:00`, with automatic 15-minute retry until success when `deye_tou_last_synced_date` does not match today. Features automated failure detection trigger at `03:00:00` creating a `persistent_notification` and mobile alert if TOU sync has not succeeded by 03:00. Supports manual trigger via `input_button.sync_deye_inverter_soc` / `script.sync_deye_inverter_tou` (with optional `force: true`). On successful write, updates `input_datetime.deye_tou_last_synced_date` with date and time (`YYYY-MM-DD HH:MM:SS`), persists to `/config/.deye_tou_last_sync.json`, and triggers cache write-through and refresh across `sensor.deye_tou_last_sync`, `sensor.deye_inverter_status`, and `sensor.pstryk_price_meter_dol`. Reg 108 is clamped to `BATTERY_CHARGE_CURRENT_MAX_CEILING_A` (100A max), Reg 109 is read-only, and Slot 6 in night branch targets `safe_min_soc` (15%).
- `daily_energy_price_notification` - Sends Polish multi-window dispatch summary at 22:00 specifically for **Tomorrow** (e.g. *"⚡ Pstryk: Prognoza na Jutro (piątek, 09.10)"* with EV, Magazyn, Szczyt Cenowy; clickAction opens `/dashboard-home/energia`)
- `Tesla Charging Best Window` - Triggers on `binary_sensor.pstryk_in_ev_best_window_dol`, automatically turns on Tesla charging when window begins and turns off when window ends.
- `pstryk_best_window_voice_announcement` - Speaks dynamic announcement with average price on Home Assistant Voice PE with quiet hours condition (07:30 - 22:00).
- `deye_battery_calibration_periodic_scheduler` & `deye_battery_calibration_auto_finish` - Daily 00:05 check against calibration interval (default 30 days) to run 100% BMS cell balancing cycle via `script.start_deye_battery_calibration` and restore to 90% via `script.finish_deye_battery_calibration`.

## Dynamic Solar Calculation Engine & Astronomy

**Script:** `config/solar_engine.py` (CLI & Python library based on Astral 2.2)
**Unit Tests:** `tests/unit/test_solar_engine.py` (seasonal solstices, outdoor dusk offset, coordinate boundary fallbacks, solar phase classification)
**Location Defaults:** Warsaw (52.3445°N, 21.0993°E, 322m, `Europe/Warsaw`)
**Outdoor Dusk Offset:** Default 30 minutes before astronomical sunset
**Naming Protocol:** UI-friendly labels/friendly names in Polish or PL/EN; all code configurations, entity IDs, YAML keys, and variables strictly in English.

**Entities:**
- `input_datetime.dynamic_dawn`: *Dynamiczna Pora Świtu* (default 06:00:00)
- `input_datetime.dynamic_sunrise`: *Dynamiczna Pora Wschodu* (default 06:30:00)
- `input_datetime.dynamic_sunset`: *Dynamiczna Pora Zmroku* (default 18:00:00)
- `input_datetime.dynamic_outdoor_dusk`: *Dynamiczna Pora Zmroku na Zewnątrz* (default 17:30:00)
- `sensor.dynamic_solar_phase`: Astronomical solar phase (`night`, `astronomical_twilight`, `nautical_twilight`, `dawn`, `sunrise`, `daylight`, `golden_hour`, `dusk`)
- `sensor.dynamic_solar_elevation`: Real-time elevation in degrees (°)
- `sensor.dynamic_solar_engine_status`: Engine status (`ok`, `fallback_helper`, `fallback_static`)
- `binary_sensor.dynamic_is_dark_outside`: Flag for exterior illumination
- `binary_sensor.dynamic_is_sun_up`: Solar disk elevation above horizon

**Automations:**
- `automation.sync_dynamic_solar_times`: Triggers at 00:00:05 and HA startup, evaluating solar attributes with 2-tier fallback (Tier 0 sticky memory, Tier 1 static defaults). All system automations and scripts have migrated to `input_datetime.dynamic_*`. Deprecated static helpers have been removed.

## Important Files

| File | Purpose |
|------|---------|
| `AI_DEVELOPMENT_GUIDELINES.md` | Mandatory AI development rules, planning gates, doc sync protocols |
| `llms.txt` | Standard LLM & human project index and architecture map |
| `PLACES_AND_BEHAVIORS.md` | Room-by-room entity directory & behavioral map |
| `AGENT.md` | Unified Home Assistant & ESP32/Node.js ecosystem bridge |
| `tests/runner.py` | 4-tier autonomous AI test harness CLI |
| `tests/test_registry.yaml` | Static baseline regression test catalog |
| `tests/docker-compose.test.yml` | Ephemeral test stack (HA + Mosquitto) |
| `docker-compose.yml` | Home Assistant container definition |
| `docker-zigbee2mqtt.sh` | Zigbee2MQTT start script |
| `config/solar_engine.py` | Dynamic solar & astronomical calculation engine |
| `tests/unit/test_solar_engine.py` | Unit test suite for solar calculation engine |
| `config/mqtt.yaml` | All MQTT entity configurations |
| `config/automations.yaml` | 60+ automation rules |
| `config/scripts.yaml` | Operational scripts (radio, TV, blinds, lighting) |
| `config/secrets.yaml` | Sensitive configuration |
| `data/configuration.yaml` | Zigbee2MQTT settings |

## Automated Testing Harness (Mandatory Pre-Commit Checks)

An ephemeral copy-on-write Docker sandbox (`/tmp/ha_test_sandbox`) and companion Mosquitto broker enable full integration testing without edge hardware. Requirement: Tier 2 sandbox must never contain the real `.env` or call real engine/client scripts (engine and client shell commands are stubbed):
- **Tier 0:** `python3 tests/runner.py --tier 0` (Static check_config in Docker)
- **Tier 1:** `python3 tests/runner.py --tier 1` (Hardware safety invariant rules [Tier 1A] + Unit test suites under tests/unit/ [Tier 1B])
- **Tier Unit:** `python3 tests/runner.py --tier unit` (Dedicated unit testing suite under `tests/unit/`, e.g. Pstryk engine)
- **Tier 2:** `python3 tests/runner.py --tier 2` (Dynamic state & service executions via REST/WebSocket)
- **Sandbox & Scenarios:** Hermetic Tier 2 sandbox (`tests/sandbox.py` `create_sandbox()`): writes a sandbox `.env` from `config/.env.example` with loopback inverter IP (127.0.0.1) and blank secrets; skips `.env`, `.deye_inverter_config.json` and `tesla_fleet.key`; rewrites every `shell_command` and every `command_line` calling `pstryk_engine.py`/`solarman_v5_client.py`/`deye_cloud_client.py` to `sh /config/test_stub_cmd.sh` (`tests/test_config_overlay/test_stub_cmd.sh`); replaces the MQTT config entry with `ha-test-mqtt`. Guarded by `tests/invariants/test_sandbox_hermetic.py`. Scenario field `preconditions.stub_rc` (0 = success, default; non-zero = failure) sets the stub's exit code (written by `tests/state_runner.py`). `runner.py --auto` runs Tier 2 for every impacted area; the `deye` area (keywords deye/solarman/tou/inverter) has its own deye scenarios. Known remaining network side effects: `rest.yaml` polls the public InPost API and `secrets.yaml` is copied for `!secret` lat/lon.
- **Tier 3:** `python3 tests/runner.py --tier 3` (Lovelace dashboard schema & entity binding audit)
- **Targeted Run:** `python3 tests/runner.py --auto` (Automatically diffs git, detects affected areas, runs targeted tests)
- **Gap Discovery:** `python3 tests/runner.py --discover` (Finds untested scripts and automations)
- **Scenario Synthesis:** `python3 tests/runner.py --generate <script_name>` (Self-building test scenario generator)
- **Stop Containers:** `python3 tests/runner.py --stop`


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
- `CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md` - Generic Camera AI Vision Engine (Face & ALPR) proposal
- `VOICE_AI_IMPROVEMENTS_PROPOSAL.md` - Voice AI & entity remediation proposal
- `/Users/wtrzonkowski/Desktop/private/ARCHITECTURE.md` - Technical architecture
- `/Users/wtrzonkowski/Desktop/private/HOME_AUTOMATION_ECOSYSTEM.md` - System overview
- `/Users/wtrzonkowski/Desktop/private/ISSUES.md` - Known issues
- `/Users/wtrzonkowski/Desktop/private/home_automation/` - ESP32/Node.js backend
