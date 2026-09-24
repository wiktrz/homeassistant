# AGENT.md — Unified Home Automation & Home Assistant Ecosystem

> **Project Scope:** This document defines the unified context, operational rules, component interactions, MQTT bus architecture, firmware specifications, modern hardware controllers (BoneIO, Shelly, Voice PE, TCL Google TV), and known pitfalls across `/Users/wtrzonkowski/Desktop/private/homeassistant` and `/Users/wtrzonkowski/Desktop/private/home_automation`.
>
> **Mandatory Rule:** In this project, all AI agent operations and documentation-related tasks must start by reading this file.
>
> **Key Companions:**
> - [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md) — Mandatory AI development rules, planning gates, and documentation update protocols.
> - [`llms.txt`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/llms.txt) — Global LLM & human project index and architecture map.
> - [`PLACES_AND_BEHAVIORS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/PLACES_AND_BEHAVIORS.md) — Comprehensive room-by-room entity directory and behavioral logic map.
> - [`AGENTS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENTS.md) — Core developer instructions and operational guidelines.

---

## 1. Ecosystem Overview & Topology

The overall system automates and manages a primary residence (**`local00`**) and six rental apartments (**`local01`** through **`local06`**).

```
                         ┌────────────────────────────────────────────────────────┐
                         │                Home Assistant (Docker)                 │
                         │    - Lovelace Dashboards, 60+ Automations              │
                         │    - MQTT Climate / Sensors / Scripts                  │
                         │    - Pstryk Dynamic Polish Energy Pricing              │
                         │    - Voice Assistant & TCL Google TV Multimedia        │
                         └───────────────────────┬────────────────────────────────┘
                                                 │
                 ┌───────────────────────────────┴──────────────────────────────┐
                 │                                                              │
                 ▼                                                              ▼
 ┌─────────────────────────────┐                                ┌─────────────────────────────┐
 │    Zigbee2MQTT (Docker)     │                                │   Mosquitto MQTT (1883)     │
 │ - Sonoff Zigbee 3.0 Dongle  │                                │  Central Message Bus (IoT)  │
 │ - Taras Dual Lamps (Bulbs)  │                                └──────────────┬──────────────┘
 │ - Wireless PIR & Contacts   │                                               │
 └─────────────────────────────┘                     ┌─────────────────────────┴─────────────────────────┐
                                                     │                                                   │
 ┌─────────────────────────────┐                     ▼                                                   ▼
 │    BoneIO DIN Controllers   │       ┌───────────────────────────┐                       ┌───────────────────────────┐
 │ - 32x10 Relays (230V)       │       │    nodeApi (Node.js/TS)   │                       │    ESP32 Edge Hardware    │
 │ - 8ch PWM Dimmer (24V)      │       │ - REST API (Port 3000)    │                       │ - newHeatingController    │
 │ - Door Strike & Power Relays│       │ - Config Server / Merging │                       │   (DevKit V1 / Relays)    │
 └─────────────────────────────┘       │ - Heating Routine Storage │                       │ - heatingStation          │
                                       │ - Serial Light Gateway    │                       │   (TTGO T4 / TFT Display) │
                                       └───────────────────────────┘                       └───────────────────────────┘
```

### Key Components

| Component | Repository Path | Tech Stack | Role |
|---|---|---|---|
| **Home Assistant** | `homeassistant/` | Docker (`stable`), YAML, Jinja2, Python | Central orchestration, UI dashboards, automations, dynamic pricing calculations |
| **Zigbee2MQTT** | `homeassistant/` (`data/`) | Docker (`koenkk/zigbee2mqtt`) | Zigbee bridge (Sonoff Dongle Plus V2 `ember`), motion sensors, door/window contacts, terrace dual lamps |
| **BoneIO 32x10 Relay** | `boneio-32-l-07` | DIN Hardware, REST/MQTT | 32 × 230V relays (lighting, door strike lock, protected power supplies) & 32 digital inputs |
| **BoneIO 8ch PWM Dimmer** | `boneio-dr-8ch-03-2c7fbc` | DIN Hardware, REST/MQTT | 8 × 24V PWM dimming channels for architectural LED lines, mirror backlights, and kitchen spots |
| **Voice PE Satellite** | `homeassistant/` | ESPHome, Nabu Casa Cloud | Smart speaker & mic satellite (`media_player.home_assistant_voice_0a9bfd_media_player`), stateful radio & TTS |
| **Living Room Multimedia**| `homeassistant/` | Android TV Remote, Cast | Salon TCL Google TV (`remote.salon_2`, `media_player.salon_2`), 6 app launchers, camera HLS casting |
| **Node API** | `home_automation/nodeApi/` | TypeScript, Node.js, Express, MQTT.js | Dynamic JSON configuration generation, heating routine resolution, serial light bridge (`/dev/ttyUSB1`) |
| **Heating Controller** | `home_automation/` (`esp32doit-devkit-v1`) | C++, PlatformIO, DallasTemperature | Multi-zone PID heating controller, OneWire DS18B20 sensors, GPIO relay actuators |
| **Heating Station** | `home_automation/` (`esp32dev_ttgo_t4`) | C++, PlatformIO, TFT_eSPI, CircleViews | Room thermostats with ILI9341 display, target temperature setpoints, routine schedule display |
| **Pstryk Engine** | `homeassistant/config/pstryk_engine.py` & `pstryk_pricing.py` | Python 3, `urllib.request` | Dynamic Polish hourly energy pricing & multi-period backend aggregation engine (`dol` & `gora`), 15-min smart caching in `/tmp/pstryk_cache_{installation}.json`, 5-dataset fetch (latest, forward 48h, today hourly, month daily, year monthly), prosumer selling tariffs, consolidated backward-compatible schema |
| **Solar Engine** | `homeassistant/config/solar_engine.py` & `sync_dynamic_solar_times` | Python 3, Astral 2.2, Jinja2 | Dynamic solar calculation engine (dawn, sunrise, solar noon, sunset, dusk, outdoor dusk) with 3-tier fallback, Polish UI labels, English entity IDs (`input_datetime.dynamic_*`) |

---

## 2. Communication & Protocols

### Bootstrapping Flow
1. ESP32 device boots, connects to WiFi (`WKTR_IOT`).
2. Performs HTTP GET request to `nodeApi` at `http://10.20.2.111:3000/config/{thermostat|heating}/{MAC}`.
3. `nodeApi` dynamically merges:
   - Base device configuration (`config/{thermostat|heating}/{MAC}.json`)
   - Apartment thermostat mapping (`config/thermostat/{MAC}.json`)
   - Heating routine schedules (`config/heatingRoutines/{MAC}_{THERMOSTAT}.json` or default routines from `heatingPlanProvider.ts`)
4. ESP32 parses JSON into memory, initializes OneWire buses, PIDs, and displays.
5. Device connects to Mosquitto MQTT broker on `10.20.2.111:1883`.

### Core MQTT Topic Reference

| Topic | Publisher | Subscriber(s) | Payload Format | Notes |
|---|---|---|---|---|
| `heating_controller/{name}/power` | `newHeatingController` | HA / Monitoring | `"on"` / `"off"` | Controller status |
| `heating_controller/+/local0X/{Room}/state` | `newHeatingController` | HA (`mqtt.yaml`) | `"ON"` / `"OFF"` | Relay state for room heating |
| `{flat}/thermostat/{Room}/routine` | `newHeatingController` | HA (`mqtt.yaml`) | String (e.g. `"defaultBedroom"`) | Active routine name |
| `room0X_{room}/sensor/temperature/state` | `newHeatingController` | `heatingStation`, HA | Float string (`"21.25"`) | Physical sensor reading |
| `{flat}/{room}/details` | `heatingStation` | HA Climate entities | JSON Object (`temperature`, `setpoint`, `mode`, `routineName`) | Aggregated climate state |
| `{flat}/thermostat/{room}/temperature/set` | HA Climate UI | `nodeApi`, `heatingStation` | Float string (`"21.5"`) | Target temperature change |
| `{flat}/thermostat/{room}/routine/set` | HA Automations | `nodeApi` | String (routine name) | Changes schedule for apartment |
| `{flat}/configuration/update` | `nodeApi` | `heatingStation` | Empty string `""` | Forces config re-fetch |
| `update/{board}/{entry}` | OTA CLI (`updater.py`) | ESP32 devices | Binary filename | Triggers OTA HTTP flash |
| `{room}/{mode}/power/update` | HA / External | `nodeApi` | `"1"` (ON) or `"0"` (OFF) | Serial light switch (`/dev/ttyUSB1`) |

---

## 3. Modern Hardware Subsystems & Operational Rules

### 3.1 BoneIO Hardware & Protected Power Supply Relays

The installation uses industrial BoneIO DIN rail hardware:
- **BoneIO 32x10 Relay Board (`boneio-32-l-07`):**
  - Oświetlenie 230V:
    - `light.boneio_32_l_07_new_light_01`: Wejście (Wiatrołap Główne)
    - `light.boneio_32_l_07_new_light_05`: Kuchnia Wyspa
    - `light.boneio_32_l_07_new_light_06`: Jadalnia Główne
    - `light.boneio_32_l_07_new_light_08`: Sypialnia Dekoracyjne
    - `light.boneio_32_l_07_new_light_09`: Pralnia Główne
    - `light.boneio_32_l_07_new_light_10`: Duża Łazienka Główne
    - `light.boneio_32_l_07_new_light_12`: Podjazd Główne
    - `light.boneio_32_l_07_new_light_14`: Pokój Nikoli Główne
    - `light.boneio_32_l_07_new_light_16`: Pokój Klary Główne
    - `light.boneio_32_l_07_new_light_17`: Sypialnia Główne
    - `light.boneio_32_l_07_new_light_19`: Gabinet Nocne
- **Protected Power Supplies (Zasilacze Oświetlenia — EXCLUDED from all-lights-off scripts):**
  - Four 230V relays feed transformers and LED drivers. Cutting mains power to these drivers disrupts downstream circuits and smart controller standby power.
  - `light.boneio_32_l_07_new_light_18` / `switch.zasilanie_salon_dodatkowe`
  - `light.boneio_32_l_07_new_light_07` / `switch.zasilanie_sypialnia_garderoba`
  - `light.boneio_32_l_07_new_light_20` / `switch.zasilanie_mala_lazienka`
  - `light.boneio_32_l_07_new_light_11` / `switch.zasilanie_taras`
- **BoneIO 8-Channel PWM Dimmer (`boneio-dr-8ch-03-2c7fbc`):**
  - `light.boneio_dr_8ch_03_2c7fbc_chl_01`: Korytarz Wejściowy
  - `light.boneio_dr_8ch_03_2c7fbc_chl_02`: Korytarz Sypialnie
  - `light.boneio_dr_8ch_03_2c7fbc_chl_04`: Kuchnia Główne
  - `light.boneio_dr_8ch_03_2c7fbc_chr_01`: Korytarz Ściana
  - `light.boneio_dr_8ch_03_2c7fbc_chr_02`: Mała Łazienka Dekoracyjne
  - `light.boneio_dr_8ch_03_2c7fbc_chr_04`: Korytarz Lustro
- **Taras Dual Lamps Group (`light.taras_lampy`):**
  - Synchronizes `light.bulb_1` (*Taras Lampa Lewa*) and `light.bulb_2` (*Taras Lampa Prawa*) into a unified entity.
- **Electric Strike Lock Protection:**
  - Relay `light.boneio_32_l_07_73bbd8_door_23_relay` is wrapped in `lock.rygiel_drzwi_wejsciowych_lock` and triggered via `input_button.btn_entrance_door` with an automatic 2-minute safety turn-off timer.

---

### 3.2 Voice Assistant & Stateful Radio Playback

- **Satellite Hardware:** Home Assistant Voice PE (`media_player.home_assistant_voice_0a9bfd_media_player`).
- **Stateful Memory:** `input_select.last_radio_station` stores the current/last station (resumed when saying *"włącz radio"* without arguments; defaults to Eska Rock).
- **Supported Stations:** Eska Rock, RMF FM, Radio ZET, Antyradio, Radio 357, TOK FM, VOX FM, Polskie Radio Trójka.
- **Scripts:**
  - `script.play_radio`: Resolves station via multi-alias dictionary (normalizing raw keys like `rmf_fm` as well as natural spoken aliases like `"RMF FM"`, `"Radio ZET"`, `"357"`, `"Trójka"`), updates `input_select.last_radio_station` only when value changed, and streams audio directly via `media_player.play_media` (ESPHome Voice PE does not support `media_player.turn_on`).
  - `script.stop_radio`: Stops stream cleanly via `media_player.media_stop`.
  - `script.toggle_radio`: Smart toggle checking if Voice PE is playing -> `script.stop_radio`, otherwise -> `script.play_radio`.
  - `script.radio_volume_up` & `script.radio_volume_down`: Adjusts volume on Voice PE.
- **Voice Intents:**
  - `WlaczRadio`: Handles playing, starting, and switching stations (*"włącz radio [stacja]"*, *"włącz [stacja]"*, *"zmień stację na [stacja]"*, *"przełącz na [stacja]"*, *"switch radio to [station]"*). Dynamically responds with *"Przełączam na..."* if radio is already playing.
  - `ZatrzymajRadio`, `GlosniejRadio`, `CiszejRadio`.
- **Automations:**
  - `morning_radio_schedule`: Weekdays at `input_datetime.pora_pobudka` -> Radio ZET; Weekends at `input_datetime.pora_pobudka_weekend` -> Antyradio.
  - `radio_station_changed_auto_play`: Seamlessly switches live stream when a user selects a different station in Lovelace while radio is active; guarded by `not is_state('script.play_radio', 'on')` to prevent recursive cancellation loops.
  - `voice_pe_media_playback_intercept`: Routes standard UI Play/Pause events to radio scripts.

---

### 3.3 Multimedia & Living Room TCL Google TV

- **Primary Entities:** `media_player.salon_2`, `remote.salon_2` (Android TV Remote), `media_player.googletv8897_2` (Google Cast).
- **Streaming App Launch Grid (`script.tv_launch_app`):**
  - Netflix, YouTube, Disney+, Max, Spotify, Prime Video.
- **Live Camera HLS Casting (`script.tv_show_camera`):**
  - Casts live streams from 5 cameras (`podworko`, `wejscie`, `taras`, `ogrod`, `front`) to `media_player.googletv8897_2`.
  - Terminates feed when passed `camera: stop`.
- **Cinema Mode (`script.salon_cinema_mode`):**
  - Turns on TV, activates `scene.salon_kino_1`. If after dusk, sets `input_select.salon_kolor_wybor` to option 2 and turns off overhead fixture `light.salon_mqtt`.

---

## 4. Important Architectural Constraints & Known Issues

### Home Assistant MQTT Configuration Rules
- **NEVER use `object_id` in `config/mqtt.yaml`**: Home Assistant's manual MQTT sensor schema does NOT support `object_id`. Only `unique_id:` and `name:` are allowed.
- **Custom Polish Names vs Entity IDs**: To give Polish names without breaking existing entity IDs in automations/dashboards:
  1. Keep `name: "local00 HolSypialnia Heating State"` and `unique_id: local00_holsypialnia_heating_state`.
  2. Use `config/customize.yaml` to assign friendly names:
     ```yaml
     sensor.local00_holsypialnia_heating_state:
       friendly_name: "Hol przy Sypialniach Stan Grzania"
     ```
  3. Or rename via the Home Assistant UI (**Settings -> Devices & Services -> Entities**).

### Day Indexing Discrepancy
- C++ `tm_wday` and Node.js `Date.prototype.getDay()` use `0 = Sunday, 1 = Monday, ..., 6 = Saturday`.
- When mapping to weekday routines (`1..5`) and weekend routines (`6..7`), naive `+ 1` maps Sunday (0) to 1 (Monday) and Friday (5) to 6 (Saturday).
- **Correct Mapping:** ISO 8601 day indexing: `(day === 0) ? 7 : day`.

### ESP32 Firmware Safety & Stability
- **Null Safety in `loop()`:** Always check pointers before dereferencing (`if (MQTTA != nullptr) MQTTA->loop(); if (OWM != nullptr) OWM->run();`).
- **Memory Management:** Heap allocations with `new` in `heatingStation` are not freed during re-configuration. Do not introduce new heap leaks. Prefer stack buffers.
- **No Blocking Delays:** Never use `delay(100)` in firmware loops as it drops MQTT keepalives and blocks display redraws. Always use non-blocking `millis()` timers.
- **OneWire Non-blocking Conversion:** Call `sensors.setWaitForConversion(false)` on DallasTemperature instances to prevent 750ms synchronous blocking per bus read.

### Node.js Backend (`nodeApi`)
- **Routine Directory:** Ensure `nodeApi/config/heatingRoutines/` exists, otherwise `fs.writeFileSync` in `thermostatSet.ts` fails with `ENOENT`.
- **Default Routine Immutability:** Never mutate `defaultHeatingRoutine` in memory; always deep-clone before setting per-room temperature overrides.

### Pstryk Energy Pricing & Multi-Period Aggregation Engine
- Script `config/pstryk_pricing.py` and backend engine `config/pstryk_engine.py` fetch dynamic hourly energy prices (VAT + distribution surcharges included).
- Multi-period aggregation engine (`config/pstryk_engine.py`) queries 5 endpoints for `dol` and `gora`:
  - `temporal=latest` with `metrics=pricing,cost,meter_values,carbon` (live gross/net prices, prosumer tariffs, instant active registers, carbon footprint).
  - 48h forward pricing with `resolution=hour` (optimal contiguous cheapest window calculation with 10% rise tolerance, all_prices curve).
  - Today's data: `window_start=midnight_utc`, `window_end=now_utc`, `resolution=hour` (today's imported/exported kWh, costs, balance, and hourly breakdown).
  - Current month data: `window_start=month_start_utc`, `window_end=now_utc`, `resolution=day` (month-to-date kWh, costs, balance, daily breakdown).
  - Year data: `window_start=year_start_utc`, `window_end=now_utc`, `resolution=month` (year-to-date monthly breakdown).
- **Smart Caching:** Atomic file caching in `/tmp/pstryk_cache_{installation}.json` with 15-minute TTL for live metrics and alongside preservation of historical frames.
- **Backward-Compatible Schema:** Retains top-level `current_price`, `start`, `end`, `duration_hours`, `all_prices`, `net`, `gross`, `installation`, while introducing rich structured sub-objects: `current`, `today`, `month`, and `year`.
- **Sensors:**
  - `sensor.pstryk_price_meter_dol` & `sensor.pstryk_price_meter_gora` (15-min command_line root sensors with full payload attributes)
  - `sensor.pstryk_best_window_dol` & `sensor.pstryk_best_window_gora` (cheapest charging window range)
  - `sensor.pstryk_cena_kupno_dol` & `sensor.pstryk_cena_kupno_gora` (live gross buy rate with pricing component attributes)
  - `sensor.pstryk_cena_sprzedaz_dol` & `sensor.pstryk_cena_sprzedaz_gora` (prosumer gross sell rate with net price)
  - `sensor.pstryk_zuzycie_dzis_dol` & `sensor.pstryk_zuzycie_dzis_gora` (today's imported kWh with hourly breakdown)
  - `sensor.pstryk_koszt_dzis_dol` & `sensor.pstryk_koszt_dzis_gora` (today's PLN expense and financial balance)
  - `sensor.pstryk_zuzycie_miesiac_dol` & `sensor.pstryk_zuzycie_miesiac_gora` (month-to-date kWh with daily history)
  - `sensor.pstryk_koszt_miesiac_dol` & `sensor.pstryk_koszt_miesiac_gora` (month-to-date PLN expense with monthly breakdown)
  - `sensor.pstryk_slad_weglowy_dol` & `sensor.pstryk_slad_weglowy_gora` (live & today's g CO₂ footprint)
- **Binary Sensors:**
  - `binary_sensor.pstryk_in_best_window_dol` & `binary_sensor.pstryk_in_best_window_gora` (active cheapest window)
  - `binary_sensor.pstryk_tania_godzina_dol` & `binary_sensor.pstryk_tania_godzina_gora` (is_cheap flag)
  - `binary_sensor.pstryk_droga_godzina_dol` & `binary_sensor.pstryk_droga_godzina_gora` (is_expensive flag)
- **Lovelace Energy UI & Drill-Down Subview (`/dashboard-home/energia` & `/energia-raport`):**
  - Main view (`/energia`) features dual sections ("Pstryk Energy — Instalacja Dół" and "Pstryk Energy — Instalacja Góra") with 8 dynamic tiles each (Kupno, Sprzedaż, Najlepsze Okno, Zużycie Dziś, Koszt Dziś, Zużycie Miesiąc, Koszt Miesiąc, Ślad Węglowy) with reactive color thresholds and one-tap navigation to the detailed report.
  - Interactive reporting subview (`/dashboard-home/energia-raport`) provides native back navigation, Dół vs Góra side-by-side comparison tables, Jinja2 hourly today breakdown, daily month history, 2026 year monthly table, and 48h live history graphs.
- Automation `daily_energy_price_notification` runs at 22:00.

### Voice Assistant & Media Players (Voice PE)
- **ESPHome Action Limitations:** Entity `media_player.home_assistant_voice_0a9bfd_media_player` is an ESPHome speaker. It does **NOT** support `media_player.turn_on` or `media_player.turn_off`. Invoking either throws a runtime exception in Home Assistant. Playback MUST be initiated directly using `media_player.play_media` and stopped cleanly using `media_player.media_stop`. Volume can be managed via `volume_set`, `volume_up`, or `volume_down`.

### Dynamic Solar & Astronomical Engine (Phase 1)
- **Engine Script:** `config/solar_engine.py` (Astral 2.2 solar calculations for Warsaw coordinates: 52.3445°N, 21.0993°E, 322m).
- **Naming Rule:** UI-friendly labels/friendly names in Polish or PL/EN; code configurations, entity IDs, YAML keys, and variables strictly in English.
- **Dynamic Helpers:** `input_datetime.dynamic_dawn`, `input_datetime.dynamic_sunrise`, `input_datetime.dynamic_sunset`, `input_datetime.dynamic_outdoor_dusk`.
- **Sensors:** `sensor.dynamic_solar_phase`, `sensor.dynamic_solar_elevation`, `sensor.dynamic_solar_engine_status`, `binary_sensor.dynamic_is_dark_outside`, `binary_sensor.dynamic_is_sun_up`.
- **Synchronization Automation:** `automation.sync_dynamic_solar_times` (`sync_dynamic_solar_times`) runs daily at 00:00:05 and HA startup.
- **Resilient 3-Tier Fallback Strategy:** Tier 0 (sticky memory), Tier 1 (legacy helper inheritance `input_datetime.pora_switu`/`pora_zmroku`), Tier 2 (static defaults `06:00:00`, `06:30:00`, `18:00:00`, `17:30:00`).
- **Phase Boundary:** Dynamic helpers are calculated and tested. Existing lighting/blind automations continue referencing legacy static helpers until Phase 2 migration.

---

## 5. Automated AI Testing Harness & Ephemeral Docker Verification

An autonomous 4-tier testing harness guarantees that code changes, YAML updates, automations, scripts, and dashboards are thoroughly validated locally without affecting physical hardware or production state databases.

### 5.1 Test Architecture & Tiers
- **Ephemeral Copy-on-Write Sandbox (`/tmp/ha_test_sandbox`):** Mounts a clean copy of `config/` (excluding production sqlite databases and runtime locks). Uses in-memory SQLite (`sqlite:////tmp/ha_test_recorder.db`) and pre-seeds admin credentials and an active Long-Lived Access Token.
- **Companion Mock MQTT (`ha-test-mqtt`):** Runs an isolated `eclipse-mosquitto:alpine` broker on port 1884 to test MQTT entity publications without edge hardware dependencies.
- **The 4 Test Tiers:**
  - **Tier 0 (Static Schema):** `python3 tests/runner.py --tier 0` — Executes Home Assistant `check_config` inside a one-shot container (<3s).
  - **Tier 1 (Safety Invariants):** `python3 tests/runner.py --tier 1` — Fast pytest audit enforcing protection of BoneIO LED power supply relays, 2-minute door strike auto-off timer, and prohibition of `object_id` in `mqtt.yaml` (<1s).
  - **Tier 2 (Dynamic State & Service Execution):** `python3 tests/runner.py --tier 2` — Executes declarative YAML scenarios against the running ephemeral container via REST/WebSocket APIs, asserting entity state transitions and checking for zero exceptions in `/api/error_log`.
  - **Tier 3 (Lovelace UI & Entity Binding):** `python3 tests/runner.py --tier 3` — Statically traverses all Lovelace dashboards (`dashboard_modern_reference.yaml`, etc.), verifying card schemas and 270+ entity bindings.

### 5.2 Test CLI & Self-Building Workflow
```bash
# Automated git-diff impact testing (runs targeted area tests + invariants)
python3 tests/runner.py --auto

# Run complete 4-tier suite
python3 tests/runner.py --tier all

# Target specific room/area
python3 tests/runner.py --area salon

# Coverage gap analysis across scripts and automations
python3 tests/runner.py --discover

# Self-Building: synthesize draft scenario for an untested script
python3 tests/runner.py --generate <script_name>

# Teardown test containers
python3 tests/runner.py --stop
```

- **Static Test Catalog:** All baseline regression tests are registered in [`tests/test_registry.yaml`](tests/test_registry.yaml) with individual scenarios in [`tests/scenarios/`](tests/scenarios/).

---

## 6. Operational & Git Guidelines

- **Planning Mode Gate (Read-Only Gate):** When in `/plan`, design mode, or preparing artifacts, AI agents must remain strictly read-only. NEVER modify, create, or delete workspace files without explicit conversational user approval. Automated stop-hook approvals do NOT grant permission to execute.
- **Mandatory Self-Updating Documentation:** Any modification to code, configuration, YAML, automations, scripts, entities, or hardware MUST trigger an automatic update to documentation ([`PLACES_AND_BEHAVIORS.md`](PLACES_AND_BEHAVIORS.md), [`AGENT.md`](AGENT.md), [`AGENTS.md`](AGENTS.md), [`llms.txt`](llms.txt)) before completing the task. Follow [`AI_DEVELOPMENT_GUIDELINES.md`](AI_DEVELOPMENT_GUIDELINES.md).
- **Mandatory Automated Test Pass:** All changes must pass `python3 tests/runner.py --auto` before task completion.
- **NEVER stage (`git add`) or commit (`git commit`)** changes unless explicitly instructed with the word 'commit'.
- **NEVER git push** changes unless explicitly requested.
- **Never delete or clear optimization databases** (`learning.db` / task cache tables).
- **Sensitive Credentials**: Do not hardcode passwords or tokens in scripts. Use `.env` or `secrets.yaml`. Keep private keys (`tesla_fleet.key`) secure.

---

## 7. Related Documentation

- [`AI_DEVELOPMENT_GUIDELINES.md`](AI_DEVELOPMENT_GUIDELINES.md) — Mandatory AI development rules, planning gates, and documentation update protocols.
- [`llms.txt`](llms.txt) — Global ecosystem index & architecture roadmap.
- [`PLACES_AND_BEHAVIORS.md`](PLACES_AND_BEHAVIORS.md) — Exhaustive place-by-place matrix, entity mapping, and automation rules.
- [`AGENTS.md`](AGENTS.md) — Primary developer instructions & agent operating guidelines.
- [`tests/test_registry.yaml`](tests/test_registry.yaml) — Static regression test catalog.
- [`CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md`](CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md) — Generic Camera AI Vision Engine (Biometric Face Recognition & ALPR License Plate Recognition) proposal.
- [`VOICE_AI_IMPROVEMENTS_PROPOSAL.md`](VOICE_AI_IMPROVEMENTS_PROPOSAL.md) — Voice AI Polish intent remediation proposal.
- [`/Users/wtrzonkowski/Desktop/private/home_automation/`](/Users/wtrzonkowski/Desktop/private/home_automation/) — ESP32 firmware & Node.js backend.

