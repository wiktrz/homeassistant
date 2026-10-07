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
| **Deye Inverter Clients** | `homeassistant/config/solarman_v5_client.py` & `deye_cloud_client.py` | Python 3, zero-dependency sockets & REST | Dual-tier Deye 12kW inverter communication: local Solarman V5 Modbus-RTU over TCP (port 8899) with seamless automatic fallback to Deye Cloud OpenAPI (`https://eu1-developer.deyecloud.com`) via official POST endpoints (`device/latest`, `config/battery`, `config/tou`, `order/sys/tou/update`), zero-dependency `.env` loading, network subnet scanner, and robust support for pre-battery / no-PV operating states |
| **Solar Engine** | `homeassistant/config/solar_engine.py` & `sync_dynamic_solar_times` | Python 3, Astral 2.2, Jinja2 | Dynamic solar calculation engine (dawn, sunrise, solar noon, sunset, dusk, outdoor dusk) with 3-tier fallback, Polish UI labels, English entity IDs (`input_datetime.dynamic_*`) |
| **InPost REST Integration** | `homeassistant/config/rest.yaml` | YAML, REST API, Jinja2 | Local outdoor weather & air quality sensors from InPost Paczkomat MAR13M (`sensor.paczkomat_mar13m_*`: air index level, temperature, humidity, pressure, PM1, PM2.5, PM10) |

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
- **Salon Ceiling Lighting Helpers:**
  - `input_number.kolor_bialy_salon` (White channel level 0–255) and `input_number.kolor_czerwony_salon` (Red channel level 0–255) for MQTT overhead illumination and accent control.
- **Electric Strike Lock Protection:**
  - Relay `light.boneio_32_l_07_73bbd8_door_23_relay` is wrapped in `lock.rygiel_drzwi_wejsciowych_lock` and triggered via `input_button.btn_entrance_door` with an automatic 2-minute safety turn-off timer.

---

### 3.2 Voice Assistant & Stateful Radio Playback

- **Satellite Hardware:** Home Assistant Voice PE (`media_player.home_assistant_voice_0a9bfd_media_player`).
- **Stateful Memory:** `input_select.last_radio_station` stores the current/last station (resumed when saying *"włącz radio"* without arguments; defaults to Eska Rock).
- **Supported Stations:** Eska Rock, RMF FM, Radio ZET, Antyradio, Radio 357, TOK FM, VOX FM, Polskie Radio Trójka.
- **Scripts:**
  - `script.play_radio`: Resolves station via multi-alias dictionary (normalizing raw keys like `rmf_fm` as well as natural spoken aliases like `"RMF FM"`, `"Radio ZET"`, `"357"`, `"Trójka"`), updates `input_select.last_radio_station` only when value changed, and streams audio directly via `media_player.play_media` (ESPHome Voice PE does not support `media_player.turn_on`).
  - `script.stop_radio`: Stops stream cleanly via `media_player.media_stop`; halts active morning routine if running.
  - `script.play_morning_music`: Randomly selects and plays an audio file from dedicated folder `/config/media/morning_music/` on Voice PE.
  - `script.toggle_radio`: Smart toggle checking if Voice PE is playing -> `script.stop_radio`, otherwise -> `script.play_radio`.
  - `script.radio_volume_up` & `script.radio_volume_down`: Adjusts volume on Voice PE.
- **Voice Intents:**
  - `WlaczRadio`: Handles playing, starting, and switching stations (*"włącz radio [stacja]"*, *"włącz [stacja]"*, *"zmień stację na [stacja]"*, *"przełącz na [stacja]"*, *"switch radio to [station]"*). Dynamically responds with *"Przełączam na..."* if radio is already playing.
  - `ZatrzymajRadio`, `GlosniejRadio`, `CiszejRadio`.
- **Automations:**
  - `morning_radio_schedule`: Two-step wake-up routine: Step 1 plays random MP3 track from `/config/media/morning_music/` (~3 min) at 40% volume, Step 2 streams scheduled radio at 10% volume (Weekdays at `input_datetime.pora_pobudka` default 07:00 -> Radio ZET; Weekends at `input_datetime.pora_pobudka_weekend` default 08:15 -> Antyradio).
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

### Pstryk Energy Pricing, Multi-Window Dispatch & Battery Storage System
- **Engine Script:** `config/pstryk_pricing.py` and backend engine `config/pstryk_engine.py` fetch dynamic hourly energy prices (VAT + distribution surcharges included) and execute the dynamic battery & auto-dispatch model.
- **Hardware Integration:**
  - **Inverter:** Deye 12 kW Hybrid Inverter (`SUN-12K-SG05LP3-EU-SM2`) at dynamic IP configured via `input_text.deye_inverter_ip` (default `10.20.2.6`) and `input_number.deye_inverter_port` (default `8899`).
  - **Power Bank:** SunDeposit 16.13 kWh LiFePO4 battery pack (315 Ah, 51.2V nominal, Bluetooth BMS).
  - **Environment Variables & Secrets Management:** Configuration templates `.env.example` (repository root) and `config/.env.example` define inverter network settings (`DEYE_INVERTER_IP`, `DEYE_INVERTER_PORT`, `DEYE_LOGGER_SN`, `DEYE_INVERTER_SN`), Deye Cloud credentials (`DEYE_CLOUD_API_URL`, `DEYE_CLOUD_API_KEY`, `DEYE_CLOUD_APP_ID`, `DEYE_CLOUD_APP_SECRET`, `DEYE_CLOUD_EMAIL`, `DEYE_CLOUD_PASSWORD`), and Pstryk API keys (`PSTRYK_API_KEY_DOL`, `PSTRYK_API_KEY_GORA`). All secret tokens and keys are read dynamically from `.env` (or `config/.env`) across all python engines (`pstryk_engine.py`, `solarman_v5_client.py`, `deye_cloud_client.py`) without exposing inline CLI credentials in HA YAML, with secondary fallback to `secrets.yaml`. Real `.env` files are strictly git-ignored.
  - **Solarman V5 Client & Local Discovery:** `config/solarman_v5_client.py` provides zero-dependency direct Modbus-RTU communication wrapped in Solarman V5 TCP frames. Supports dynamic IP resolution hierarchy (explicit CLI -> env var `DEYE_INVERTER_IP` -> persistent `/config/.deye_inverter_config.json` -> HA `core.restore_state` -> `10.20.2.6`), and fast multi-threaded network scanner (`--scan`) probing port 8899 with Solarman V5 frame verification (`0xA5..0x15`).
  - **Deye Cloud OpenAPI & Dual-Tier Fallback:** `config/deye_cloud_client.py` provides zero-dependency cloud integration with European regional endpoint (`https://eu1-developer.deyecloud.com`) aligned directly with official Deye Developer OpenAPI specs (`POST /v1.0/account/token?appId={AppId}`). Generates 60-day temporary `accessToken` cached in `/tmp/deye_cloud_token.json` using Developer App credentials (`DEYE_CLOUD_APP_ID`, `DEYE_CLOUD_APP_SECRET`) combined with user account login (`DEYE_CLOUD_EMAIL`, `DEYE_CLOUD_PASSWORD`) with automatic in-memory lowercase SHA-256 hashing. If the local Wi-Fi stick (`DYDA_WiBLE_1.6.2`, SN `D26213439330`) has local port 8899 closed, `solarman_v5_client.py` and `pstryk_engine.py` automatically and transparently fall back to `deye_cloud_client.py` for read-all telemetry and TOU schedule uploads (`/v1.0/order/sys/tou/update`). CLI commands include `--get-token`, `--read-all`, `--read-soc`, and `--sync-pstryk-tou` (or `pstryk_engine.py --sync-deye-tou`).
  - **UI Configuration & Discovery:** `input_button.scan_deye_inverter_ip` triggers `script.scan_and_set_deye_inverter_ip`, discovering the inverter on the LAN and populating `input_text.deye_inverter_ip`. Automation `deye_inverter_ip_change_sync` persists user IP/port changes and re-reads status.
  - **Hardware Control Policy:** Direct writes to hardware registers 108 (Max Charge Current), 109 (Max Discharge Current), 110 (Min Discharge SOC), 111 (Shutdown SOC), and 112 (Max Charge SOC) are **strictly BLOCKED** in software. These parameters are configured manually by the user on the inverter physical screen.
  - **Read-Only Telemetry & Ingestion:** Home Assistant automatically polls the inverter in Read-Only mode every 5 minutes (`sensor.deye_inverter_status` via `--read-all`) to ingest hardware settings (108..112), live telemetry (587..591), and active TOU table (248..273).
  - **Time-of-Use (TOU) Programming:** The only write operation permitted is the 6-slot Time-of-Use schedule (registers `248..273`).
- **Dynamic 6-Slot Time-of-Use Schedule:**
  - Automatically generated by `pstryk_engine.py` based on dynamic Pstryk spot prices and live battery SOC:
    - **Slot 1 (Night Charging):** Cheapest night window (`best_pb_window` start) -> Target SOC = 100% on calibration or Max SOC (90%), Grid Charge = ON (5000W).
    - **Slot 2 (Morning Standby / Hold):** Post-charging hold -> Target SOC = Max SOC (holds charge, protects battery from discharging before peak sell hours), Grid Charge = OFF.
    - **Slot 3 (Midday PV Solar Dip / Opportunistic):** 12:00 (or midday dip in disjoint window) -> Target SOC = 80%, Grid Charge = ON if cheap dip detected else OFF.
    - **Slot 4 (Pre-Peak Standby / Hold):** 15:00 -> Target SOC = max(min_soc + 20, current_soc) (reserves battery capacity for high-export sell window).
    - **Slot 5 (Peak Evening Discharge / Sell):** `best_sell_window` start (e.g. 17:00/18:00) -> Target SOC = Min SOC (20%), Grid Charge = OFF (discharges to support home and export to grid).
    - **Slot 6 (Night Base / Standby):** `best_sell_window` end (e.g. 21:00/22:00) -> Target SOC = Min SOC (20%), Grid Charge = OFF (standby until night charging window).
- **Physical Battery Model & Operating Bounds:**
  - Upper Limit (Max Charge SOC): default 90% (read from Reg 112).
  - Lower Limit (Min Discharge SOC): default 20% (read from Reg 110).
  - Shutdown SOC: default 5% (read from Reg 111).
  - Operating usable capacity: $(90\% - 20\%) \times 16.13 = \mathbf{11.29\text{ kWh}}$.
  - Emergency blackout reserve: $(20\% - 5\%) \times 16.13 = \mathbf{2.42\text{ kWh}}$.
  - Top cell-protection buffer: $(100\% - 90\%) \times 16.13 = \mathbf{1.61\text{ kWh}}$.
  - Transfer power: 100A @ 51.2V = **5.12 kW** (time to full charge: ~2h 12m).
- **Dynamic Multi-Window Dispatch (with Average Prices in UI):**
  - **EV Charging Window (`ev_best_window`):** Weekdays 2h continuous, weekends 4h/6h continuous (`sensor.pstryk_ev_best_window_dol`, e.g. `01:00 - 03:00 (śr. 0.88 zł/kWh)`).
  - **Power Bank Window (`powerbank_best_window`):** 2h capacity, supports continuous slots (`02:00 - 04:00 (śr. 0.86 zł/kWh)`) or disjoint slots with full interval notation (`03:00 - 04:00 oraz 14:00 - 15:00 (śr. 0.79 zł/kWh)`).
  - **Peak Sell / Discharge Window (`best_sell_window`):** 3h–5h peak prosumer selling window + 1h spike (`sensor.pstryk_best_sell_window_dol`, e.g. `17:00 - 21:00 (śr. 1.34 zł/kWh, pik 19:00: 1.48 zł)`).
- **Battery Health & 100% BMS Calibration Scheduling:**
  - LiFePO4 cells require periodic 100% saturation for BMS top cell balancing and state-of-charge drift correction.
  - Managed by `input_select.deye_battery_calibration_frequency` (default: every 30 days), `input_datetime.deye_battery_last_calibration_date`, and `sensor.deye_battery_days_since_calibration`.
  - Automation `deye_battery_calibration_periodic_scheduler` triggers at 00:05 daily, sets calibration flag and recalculates TOU table with Slot 1 Target SOC = 100% via `script.start_deye_battery_calibration`.
  - When SOC reaches 100% (`sensor.deye_battery_soc > 99.5`), automation `deye_battery_calibration_auto_finish` runs `script.finish_deye_battery_calibration`, resetting calibration mode and updating the calibration date.
- **5-Dataset Multi-Period Aggregations:** `temporal=latest` (live prices, tariffs, instant active registers, carbon), 48h forward prices (cheapest window with 10% rise limit), today's hourly breakdown, current month daily breakdown, and year monthly breakdown.
- **Smart Caching:** Atomic file caching in `/tmp/pstryk_cache_{installation}.json` with 15-minute TTL for live metrics alongside preservation of historical frames.
- **Backward-Compatible Schema:** Retains top-level `current_price`, `start`, `end`, `duration_hours`, `all_prices`, `net`, `gross`, `installation`, `battery_model`, `ev_best_window`, `powerbank_best_window`, `best_sell_window`, and `deye_tou_schedule`.
- **Sensors:**
  - `sensor.deye_inverter_status` (command_line Modbus reader returning `config`, `telemetry`, `active_tou`)
  - `sensor.deye_inverter_max_charge_soc`, `min_discharge_soc`, `shutdown_soc` (read-only ingested hardware parameters)
  - `sensor.deye_inverter_max_charge_current`, `max_discharge_current` (read-only ingested current limits)
  - `sensor.deye_battery_soc`, `voltage`, `power`, `current` (live telemetry parsed from registers 587..591)
  - `sensor.deye_tou_active_schedule` (active 6-slot schedule state and attributes)
  - `sensor.pstryk_price_meter_dol` & `sensor.pstryk_price_meter_gora` (15-min command_line root sensors with full payload attributes)
  - `sensor.pstryk_best_window_dol` & `sensor.pstryk_best_window_gora` (cheapest charging window range)
  - `sensor.pstryk_ev_best_window_dol` & `_avg_price_dol` (EV charging window & average price)
  - `sensor.pstryk_powerbank_best_window_dol` & `_avg_price_dol` (Power Bank window & average price)
  - `sensor.pstryk_best_sell_window_dol`, `_avg_price_dol`, & `sensor.pstryk_peak_sell_spike_dol` (Sell window, avg price, 1h spike)
  - `sensor.deye_battery_operating_capacity_kwh` & `sensor.deye_battery_blackout_reserve_kwh` (11.29 kWh & 2.42 kWh dynamic)
  - `sensor.deye_charge_power_kw` & `sensor.deye_discharge_power_kw` (5.12 kW transfer rates dynamic)
  - `sensor.deye_battery_display_level` (% and kWh representation, e.g. `85% (13.7 kWh)` or `"Oczekuje na montaż (SunDeposit 16.13 kWh)"` when uninstalled)
  - `sensor.deye_battery_working_state` (state readout: `Ładowanie`, `Rozładowanie (Sprzedaż)`, `Czuwanie`, or `"Brak baterii (czuwanie)"` in pre-battery phase)
  - `sensor.deye_grid_power` & `sensor.deye_consumption_power` (live 3-phase grid import and total household load in Watts, e.g. `482 W`)
  - `sensor.deye_daily_consumption` & `sensor.deye_daily_energy_purchased` (real-time daily energy counters from physical inverter CT clamps in kWh)
  - `sensor.deye_inverter_temperature` (live inverter AC thermal sensor, e.g. `32.3 °C`)
  - `input_boolean.deye_battery_installed` (hardware state flag, default `off`, auto-detected when DC bus voltage > 40V)
  - `sensor.deye_battery_days_since_calibration` (days counter since last 100% BMS calibration)
  - `sensor.pstryk_cena_kupno_dol` & `sensor.pstryk_cena_kupno_gora` (live gross buy rate with pricing components)
  - `sensor.pstryk_cena_sprzedaz_dol` & `sensor.pstryk_cena_sprzedaz_gora` (prosumer gross sell rate with net price)
  - `sensor.pstryk_zuzycie_dzis_dol` & `sensor.pstryk_zuzycie_dzis_gora` (today's imported kWh with hourly breakdown)
  - `sensor.pstryk_koszt_dzis_dol` & `sensor.pstryk_koszt_dzis_gora` (today's PLN expense and financial balance)
  - `sensor.pstryk_zuzycie_miesiac_dol` & `sensor.pstryk_zuzycie_miesiac_gora` (month-to-date kWh with daily history)
  - `sensor.pstryk_koszt_miesiac_dol` & `sensor.pstryk_koszt_miesiac_gora` (month-to-date PLN expense with monthly breakdown)
  - `sensor.pstryk_slad_weglowy_dol` & `sensor.pstryk_slad_weglowy_gora` (live & today's g CO₂ footprint)
- **Binary Sensors:**
  - `binary_sensor.pstryk_in_best_window_dol` & `binary_sensor.pstryk_in_best_window_gora` (active cheapest window)
  - `binary_sensor.pstryk_in_ev_best_window_dol` (active EV charging window)
  - `binary_sensor.pstryk_in_powerbank_best_window_dol` (active Power Bank charging window)
  - `binary_sensor.pstryk_in_sell_window_dol` (active peak selling window)
  - `binary_sensor.pstryk_tania_godzina_dol` & `binary_sensor.pstryk_tania_godzina_gora` (is_cheap flag)
  - `binary_sensor.pstryk_droga_godzina_dol` & `binary_sensor.pstryk_droga_godzina_gora` (is_expensive flag)
- **Lovelace Dashboards:**
  - **Overview (`/dashboard-home/dom`):** Box `Energia i Info` displays Koszt Dziś (Dół & Góra), Okno EV (aktywne - `binary_sensor.pstryk_in_ev_best_window_dol`), kondycjonalnie Magazyn SunDeposit poziom (`%` i `kWh`) oraz stan pracy tri-state (ukryte dopóki bateria nie jest zamontowana: `input_boolean.deye_battery_installed == 'on'`), Najlepsze Okno Sprzedaży ze średnią ceną, oraz termin wywozu odpadów.
  - **Energy View (`/dashboard-home/energia`):** Features 4 dedicated sections: 1. Harmonogram Dyspozytorski Pstryk (EV, Magazyn, Sprzedaż z cenami średnimi i kafelkami statusu), 2. Magazyn Energii SunDeposit 16.13 kWh & Falownik Deye 12 kW (poziom, tryb, pojemność użyteczna, rezerwa blackout, moc), 3. Parametry Odczytane z Falownika Deye (Modbus Read-Only) & Harmonogram Time-of-Use (TOU 6 slotów, markdown tabela), 4. Zdrowie Baterii i Kalibracja BMS 100% z polem tylko do odczytu `Trwa Procedura Kalibracji 100%` (`binary_sensor.deye_battery_calibration_active`, tap_action: none) sterowanym przyciskiem ręcznym `input_button.trigger_deye_battery_calibration`, obok 16 kafelków instalacji Dół/Góra (zoptymalizowane `type: tile` z `command_timeout: 45`).
  - **Reporting Subview (`/dashboard-home/energia-raport`):** Subview with native back navigation, Dół vs Góra side-by-side comparison tables, Jinja2 hourly today breakdown, daily month history, 2026 year monthly table, and 48h live history graphs.
- **Automations & Scripts:**
  - `deye_tou_schedule_4x_daily`: Recalculates and uploads 6-slot TOU schedule 4 times a day (`00:05`, `06:00`, `14:15`, `20:00`) and on manual button press via `script.sync_deye_inverter_tou`.
  - `daily_energy_price_notification`: 22:00 forecast notification with multi-window dispatch summary (EV, Magazyn, Sprzedaż) for the next day.
  - `Tesla Charging Best Window`: Automatically starts charging (`switch.tesla_y_charge`) when `binary_sensor.pstryk_in_ev_best_window_dol == on` and turns off when window ends.
  - `pstryk_best_window_voice_announcement`: Speaks aloud dynamically on Home Assistant Voice PE with quiet hours guard (07:30–22:00).
  - `deye_battery_calibration_periodic_scheduler` & `deye_battery_calibration_auto_finish`: Automated 100% BMS balancing cycle.


### Voice Assistant & Media Players (Voice PE)
- **ESPHome Action Limitations:** Entity `media_player.home_assistant_voice_0a9bfd_media_player` is an ESPHome speaker. It does **NOT** support `media_player.turn_on` or `media_player.turn_off`. Invoking either throws a runtime exception in Home Assistant. Playback MUST be initiated directly using `media_player.play_media` and stopped cleanly using `media_player.media_stop`. Volume can be managed via `volume_set`, `volume_up`, or `volume_down`.

### Dynamic Solar & Astronomical Engine
- **Engine Script:** `config/solar_engine.py` (Astral 2.2 solar calculations for Warsaw coordinates: 52.3445°N, 21.0993°E, 322m).
- **Naming Rule:** UI-friendly labels/friendly names in Polish or PL/EN; code configurations, entity IDs, YAML keys, and variables strictly in English.
- **Dynamic Helpers:** `input_datetime.dynamic_dawn`, `input_datetime.dynamic_sunrise`, `input_datetime.dynamic_sunset`, `input_datetime.dynamic_outdoor_dusk`.
- **Sensors:** `sensor.dynamic_solar_phase`, `sensor.dynamic_solar_elevation`, `sensor.dynamic_solar_engine_status`, `binary_sensor.dynamic_is_dark_outside`, `binary_sensor.dynamic_is_sun_up`.
- **Synchronization Automation:** `automation.sync_dynamic_solar_times` (`sync_dynamic_solar_times`) runs daily at 00:00:05 and HA startup.
- **Resilient Fallback Strategy:** Tier 0 (sticky memory), Tier 1 (static defaults `06:00:00`, `06:30:00`, `18:00:00`, `17:30:00`).
- **Phase 2 Migration Complete:** All active lighting, blind, entrance reed, and cinema automations have been migrated to the dynamic helpers. Deprecated static helpers (`pora_switu`, `pora_zmroku`, `pora_zmroku_na_zewnatrz`) have been removed from configuration.

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

