# AGENT.md — Unified Home Automation & Home Assistant Ecosystem

> **Project Scope:** This document defines the unified context, operational rules, component interactions, MQTT bus architecture, firmware specifications, and known pitfalls across `/Users/wtrzonkowski/Desktop/private/homeassistant` and `/Users/wtrzonkowski/Desktop/private/home_automation`.
>
> **Mandatory Rule:** In this project, all AI agent operations and documentation-related tasks must start by reading this file.

---

## 1. Ecosystem Overview & Topology

The overall system automates and manages a primary residence (**`local00`**) and six rental apartments (**`local01`** through **`local06`**).

```
                        ┌──────────────────────────────────────────────┐
                        │              Home Assistant (Docker)         │
                        │    - Lovelace Dashboards, 60+ Automations    │
                        │    - MQTT Climate / Sensors / Scripts        │
                        │    - Pstryk Dynamic Electricity Pricing      │
                        └──────────────────────┬───────────────────────┘
                                               │
               ┌───────────────────────────────┴──────────────────────────────┐
               │                                                              │
               ▼                                                              ▼
┌─────────────────────────────┐                                ┌─────────────────────────────┐
│    Zigbee2MQTT (Docker)     │                                │   Mosquitto MQTT (1883)     │
│ - Sonoff Zigbee 3.0 Dongle  │                                │  Central Message Bus (IoT)  │
│ - PIR motion, reed sensors  │                                └──────────────┬──────────────┘
│ - Shelly RGBW2 LED strips   │                                               │
└─────────────────────────────┘                     ┌─────────────────────────┴─────────────────────────┐
                                                    │                                                   │
                                                    ▼                                                   ▼
                                      ┌───────────────────────────┐                       ┌───────────────────────────┐
                                      │    nodeApi (Node.js/TS)   │                       │    ESP32 Edge Hardware    │
                                      │ - REST API (Port 3000)    │                       │ - newHeatingController    │
                                      │ - Config Server / Merging │                       │   (DevKit V1 / Relays)    │
                                      │ - Heating Routine Storage │                       │ - heatingStation          │
                                      │ - Serial Light Gateway    │                       │   (TTGO T4 / TFT Display) │
                                      └───────────────────────────┘                       └───────────────────────────┘
```

### Key Components

| Component | Repository Path | Tech Stack | Role |
|---|---|---|---|
| **Home Assistant** | `homeassistant/` | Docker (`stable`), YAML, Jinja2, Python | Central orchestration, UI dashboards, automations, dynamic pricing calculations |
| **Zigbee2MQTT** | `homeassistant/` (`data/`) | Docker (`koenkk/zigbee2mqtt`) | Zigbee bridge (Sonoff Dongle Plus V2 `ember`), motion sensors, door/window contacts |
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

## 3. Important Architectural Constraints & Known Issues

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
- Binary sensors: `binary_sensor.pstryk_in_best_window_dol` and `binary_sensor.pstryk_in_best_window_gora` trigger automated charging and heating boosts.
- Automation `daily_energy_price_notification` runs at 22:00.

---

## 4. Operational & Git Guidelines

- **NEVER stage (`git add`) or commit (`git commit`)** changes unless explicitly instructed with the word 'commit'.
- **NEVER git push** changes unless explicitly requested.
- **Never delete or clear optimization databases** (`learning.db` / task cache tables).
- **Sensitive Credentials**: Do not hardcode passwords or tokens in scripts. Use `.env` or `secrets.yaml`. Keep private keys (`tesla_fleet.key`) secure.
