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
| **Pstryk Engine** | `homeassistant/config/pstryk_pricing.py` | Python 3, `urllib.request` | Dynamic Polish hourly energy pricing (dół & góra meters), cheapest window optimization |

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
- **Scripts:** `script.play_radio`, `script.stop_radio`, `script.toggle_radio`, `script.radio_volume_up`, `script.radio_volume_down`.
- **Automations:**
  - `morning_radio_schedule`: Weekdays at `input_datetime.pora_pobudka` -> Radio ZET; Weekends at `input_datetime.pora_pobudka_weekend` -> Antyradio.
  - `radio_station_changed_auto_play`: Seamlessly switches live stream when a user selects a different station in Lovelace while radio is active.
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

### Pstryk Energy Pricing
- Script `config/pstryk_pricing.py` fetches dynamic hourly energy prices (VAT + distribution surcharges included).
- Sensors: `sensor.pstryk_price_meter_dol` and `sensor.pstryk_price_meter_gora`.
- Binary sensors: `binary_sensor.pstryk_in_best_window_dol` and `binary_sensor.pstryk_in_best_window_gora` trigger automated charging (`switch.tesl_y_charge`) and spoken alerts (`pstryk_best_window_voice_announcement`).
- Automation `daily_energy_price_notification` runs at 22:00.

### Voice Assistant & Media Players (Voice PE)
- **ESPHome Action Limitations:** Entity `media_player.home_assistant_voice_0a9bfd_media_player` is an ESPHome speaker. It does **NOT** support `media_player.turn_on` or `media_player.turn_off`. Invoking either throws a runtime exception in Home Assistant. Playback MUST be initiated directly using `media_player.play_media` and stopped cleanly using `media_player.media_stop`. Volume can be managed via `volume_set`, `volume_up`, or `volume_down`.

---

## 5. Operational & Git Guidelines

- **Planning Mode Gate (Read-Only Gate):** When in `/plan`, design mode, or preparing artifacts, AI agents must remain strictly read-only. NEVER modify, create, or delete workspace files without explicit conversational user approval. Automated stop-hook approvals do NOT grant permission to execute.
- **Mandatory Self-Updating Documentation:** Any modification to code, configuration, YAML, automations, scripts, entities, or hardware MUST trigger an automatic update to documentation ([`PLACES_AND_BEHAVIORS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/PLACES_AND_BEHAVIORS.md), [`AGENT.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENT.md), [`AGENTS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENTS.md), [`llms.txt`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/llms.txt)) before completing the task. Follow [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md).
- **NEVER stage (`git add`) or commit (`git commit`)** changes unless explicitly instructed with the word 'commit'.
- **NEVER git push** changes unless explicitly requested.
- **Never delete or clear optimization databases** (`learning.db` / task cache tables).
- **Sensitive Credentials**: Do not hardcode passwords or tokens in scripts. Use `.env` or `secrets.yaml`. Keep private keys (`tesla_fleet.key`) secure.

---

## 6. Related Documentation

- [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md) — Mandatory AI development rules, planning gates, and documentation update protocols.
- [`llms.txt`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/llms.txt) — Global ecosystem index & architecture roadmap.
- [`PLACES_AND_BEHAVIORS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/PLACES_AND_BEHAVIORS.md) — Exhaustive place-by-place matrix, entity mapping, and automation rules.
- [`AGENTS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENTS.md) — Primary developer instructions & agent operating guidelines.
- [`CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md) — Generic Camera AI & Biometric Face Recognition engine proposal.
- [`VOICE_AI_IMPROVEMENTS_PROPOSAL.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/VOICE_AI_IMPROVEMENTS_PROPOSAL.md) — Voice AI Polish intent remediation proposal.
- [`/Users/wtrzonkowski/Desktop/private/home_automation/`](file:///Users/wtrzonkowski/Desktop/private/home_automation/) — ESP32 firmware & Node.js backend.
