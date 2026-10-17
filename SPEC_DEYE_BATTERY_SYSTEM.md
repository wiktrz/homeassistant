# SPEC_DEYE_BATTERY_SYSTEM.md — Deye Hybrid Inverter & Battery Storage System Specification

> **Status:** Active & Mandatory Architecture Specification  
> **Target Hardware:** Deye 12 kW Hybrid Inverter (`SUN-12K-SG05LP3-EU-SM2`, SN `2603160727`, Station `Parkowa13a`) + LiFePO4 Battery Storage (dynamic capacity, 16S cells topology, Bluetooth BMS)  
> **Key Companions:**
> - [`AI_DEVELOPMENT_GUIDELINES.md`](AI_DEVELOPMENT_GUIDELINES.md) — Mandatory development rules & planning gates.
> - [`AGENT.md`](AGENT.md) — Ecosystem bridge and MQTT/hardware architecture.
> - [`AGENTS.md`](AGENTS.md) — Developer guidelines and operations map.
> - [`PLACES_AND_BEHAVIORS.md`](PLACES_AND_BEHAVIORS.md) — Room & entity behavioral matrix.
> - [`llms.txt`](llms.txt) — Global index.

---

## 1. Core Architectural Invariants

### 1.1 Zero Hardcoding Rule (Strict Invariant)
Hardware parameters of the battery storage system must **NEVER** be hardcoded anywhere in the repository (including Home Assistant YAML, Jinja templates, shell commands, Python engines, automations, scripts, or dashboards):
- **Battery Capacity:** Must be dynamically read from Deye Holding Register `102` (`REG_BATT_CAPACITY` in Ah) or Deye Cloud OpenAPI (`battCapacity`), dynamically computing `batt_capacity_kwh = (batt_capacity * nominal_voltage) / 1000.0`.
- **Nominal Voltage:** Must be dynamically resolved from `.env` (`BATTERY_NOMINAL_VOLTAGE`), live battery voltage (Register 587 > 40V), or Deye Cloud config.
- **Maximum Charge/Discharge Current:** Must be dynamically read from Deye Holding Register `108` (`REG_MAX_CHARGE_CURRENT`) and Register `109` (`REG_MAX_DISCHARGE_CURRENT`).
- **Battery Model String:** Must be dynamically composed from actual capacity and Ah (e.g. `Magazyn LiFePO4 {{ cap }} kWh ({{ ah }} Ah)`), never statically hardcoded.
- **Continuous Enforcement:** Continuously verified and blocked by pytest invariant `tests/invariants/test_safety_rules.py::test_no_hardcoded_battery_parameters`.

### 1.2 Read-Only Hardware Protection (Registers 108..112)
Direct writes to critical system registers on the inverter are **strictly blocked** in software:
- Register `108`: Max Charge Current (A)
- Register `109`: Max Discharge Current (A)
- Register `110`: Min Discharge SOC / Capacity Grid-tied lower bound (%)
- Register `111`: Shutdown SOC / Critical Blackout cut-off (%)
- Register `112`: Max Charge SOC (%)

These limits represent physical hardware boundaries and must be set manually on the inverter physical LCD screen. Home Assistant ingests all parameters in **Read-Only** mode via `sensor.deye_inverter_status`.

### 1.3 Permitted Write Operations
The only write operation permitted from Home Assistant / CLI is the **6-slot Time-of-Use (TOU) Schedule** (Modbus Holding Registers `248..273` or Deye Cloud OpenAPI `/v1.0/order/sys/tou/update`).

### 1.4 Single Source of Truth & Pre-Check Protocol (Rule 10)
- **Centralized Fallback Storage:** Fallback values (e.g. 16.13 kWh capacity, 51.2 V nominal voltage, 100.0 A max charge current, default IP `10.20.2.6`, port `8899`) reside in **EXACTLY ONE PLACE**: the environment configuration template (`.env.example` / `.env`).
- **Zero Hardcoded Literals in Python & YAML:** Python modules (`config/energy_defaults.py`, `config/pstryk_engine.py`, `config/solarman_v5_client.py`, `config/deye_cloud_client.py`) and YAML configurations must NEVER declare inline numeric fallback literals. All code must import and resolve defaults through `config/energy_defaults.py`, which dynamically parses `.env` / `.env.example`.
- **Pre-Check Tool:** Before defining any new constant or helper, agents must run `python3 scripts/check_duplicate_variable.py --search "<term>"` to verify if a candidate already exists.
- **Continuous Invariant Protection:** Enforced continuously by `scripts/check_duplicate_variable.py --validate` and `tests/invariants/test_no_duplicate_defaults.py`.

---

## 2. Inverter Telemetry & Register Map

### 2.1 Modbus RTU over Solarman V5 TCP (Port 8899)
- **Slave ID:** `1`
- **Function Codes:** `0x03` (Read Holding Registers), `0x06` (Write Single Register)
- **Framing:** Solarman V5 header (`0xA5` ... `0x15`) with CRC16 Modbus checksum.

| Register | Address (Hex) | Description | Unit / Format | Access |
|:---:|:---:|:---|:---:|:---:|
| `102` | `0x0066` | Battery Capacity | Ah (integer) | Read-Only |
| `108` | `0x006C` | Max Charge Current | A (integer) | Read-Only |
| `109` | `0x006D` | Max Discharge Current | A (integer) | Read-Only |
| `110` | `0x006E` | Min Discharge SOC (Grid Cutoff) | % (integer) | Read-Only |
| `111` | `0x006F` | Shutdown SOC (Blackout Cutoff) | % (integer) | Read-Only |
| `112` | `0x0070` | Max Charge SOC | % (integer) | Read-Only |
| `248` | `0x00F8` | TOU Master Enable | 1 = Enabled, 0 = Disabled | Read / Write |
| `250..255` | `0x00FA..FF` | Slot 1..6 Start Time | `HH * 100 + MM` | Read / Write |
| `256..261` | `0x0100..05` | Slot 1..6 Power Limit | Watts (W) | Read / Write |
| `262..267` | `0x0106..0B` | Slot 1..6 Target SOC | % (integer) | Read / Write |
| `268..273` | `0x010C..11` | Slot 1..6 Grid Charge Flag | 1 = On, 0 = Off | Read / Write |
| `587` | `0x024B` | Live Battery Voltage | 0.1 V | Read-Only |
| `588` | `0x024C` | Live Battery SOC | % (integer) | Read-Only |
| `590` | `0x024E` | Live Battery Power | Watts (signed 16-bit: + discharge, - charge) | Read-Only |
| `591` | `0x024F` | Live Battery Current | 0.1 A (signed 16-bit) | Read-Only |

### 2.2 Deye Cloud OpenAPI (`https://eu1-developer.deyecloud.com`)
Fallback when local port 8899 is closed or Wi-Fi logger drops.
- `POST /v1.0/account/token?appId={AppId}` — 60-day OAuth token via App ID, Secret, Email, SHA-256 hashed password.
- `POST /v1.0/device/latest` — Live 3-phase telemetry, AC temp, battery voltage, SOC, powers.
- `POST /v1.0/config/battery` — Read `battCapacity`, `maxChargeCurrent`, `battLowCapacity`, `battShutDownCapacity`.
- `POST /v1.0/config/tou` — Read active 6-slot TOU table.
- `POST /v1.0/order/sys/tou/update` — Write 6-slot TOU table (`HH:mm`, `power`, `capacity`, `enableGridCharge`, `enableSell: false`, `enableGeneration: false`).

---

## 3. Persistent Caching & Rate-Limiting Specification

To protect the Deye Wi-Fi logger and SD card / flash storage, all Deye reads implement an intelligent write-on-change caching layer:

1. **Cache File Location:** `/config/.deye_inverter_cache.json` (fallback `/tmp/.deye_inverter_cache.json`).
2. **Rate-Limiting (30s TTL):** In-memory and file check. If `read_inverter_telemetry()` is called within 30 seconds of the previous successful poll, it immediately returns the cached payload with `"source": "cache"` without hitting the network. Bypassed via `--force-refresh` or `force_refresh=True`.
3. **Write-on-Change Semantic Diff:** Disk writes occur **strictly when telemetry or config actually changes**. The `are_deye_readings_identical(new, old)` comparator evaluates `config`, `telemetry`, and `active_tou` while ignoring volatile timestamps. If readings match, zero disk I/O occurs, preserving flash storage.
4. **Resilient Offline Fallback:** When inverter network times out or drops packets, the engine automatically serves the last known cache with `"offline": true` and `"source": "cached_fallback"`. This prevents Home Assistant sensors from flipping to `unavailable` or `unknown` during transient Wi-Fi blips.

---

## 4. Dynamic Time-of-Use (TOU) Dispatch Algorithm

### 4.1 Two Dynamic Peaks Strategy
The 6 slots are partitioned across two daily energy price peaks:
- **Slot 1 (00:00 – 06:00):** Holds dynamic morning reserve:
  $$\text{morning\_reserve\_soc} = \min(\text{max\_soc}, \text{min\_soc} + \text{margin})$$
  Default margin: +10% (`input_number.deye_morning_reserve_margin_pct`).
- **Slot 2 (06:00 – 10:00):** Morning Peak Autoconsumption. Sets target SOC to `min_soc`, powering household loads from battery during the morning price spike without buying peak grid energy.
- **Slot 3 (Cheapest Spot Dip, e.g. 11:00 – 15:00):** Grid / Solar Charging Window. Sets target SOC to `max_soc` with `grid_charge = True`.
- **Slot 4 (Pre-Peak Afternoon):** Daytime Autoconsumption. Sets target SOC to partial reserve $\max(\text{min\_soc} + 20, \text{max\_soc} - 20)$, preventing excessive battery drain before the evening peak while avoiding frozen hold.
- **Slot 5 (17:00 – 21:00):** Peak Evening Discharge. Sets target SOC to `min_soc` at full base power $P_{\text{base}}$ to eliminate all grid imports during the highest spot prices.
- **Slot 6 (21:00 – 24:00):** Night Standby. Holds `morning_reserve_soc` until midnight.

### 4.2 Energy Delta Aware Power Scaling with +10% Loss Buffer
When charging window duration $T$ extends beyond 2 hours (e.g. 3h–5h on weekends), charging power dynamically scales to match the exact energy required to reach `max_soc` from `current_soc`:
1. $$\Delta SOC = \max(0, \text{max\_soc} - SOC_{\text{current}})$$
2. $$\Delta E = \left(\frac{\Delta SOC}{100}\right) \times \text{Capacity}_{\text{kWh}}$$
3. Incorporate **+10% loss buffer** to account for AC/DC inverter conversion, BMS overhead, and LiFePO4 cell heating:
   $$E_{\text{charge}} = \Delta E \times 1.10$$
4. Calculate required power:
   $$P_{\text{calc}} = \frac{E_{\text{charge}} \times 1000}{T}$$
5. Clamp charging power to safe bounds:
   $$P_{\text{charge}} = \max\left(1000\text{ W}, \min(P_{\text{base}}, \operatorname{round}(P_{\text{calc}}, -1))\right)$$
   where $P_{\text{base}} = \min(12000, \max(1000, \operatorname{round}(I_{\text{max\_charge}} \times V_{\text{nominal}}, -1)))$.
6. **Slot 5 Protection:** Slot 5 (Peak Evening Discharge) strictly remains at full base power $P_{\text{base}}$ regardless of window duration.

### 4.3 BMS Calibration Routine (100% Equalization)
Triggered via `script.trigger_deye_battery_calibration`:
- Overrides `max_soc` to strictly `100%`.
- Sets charging power to $\max(3500\text{ W}, P_{\text{base}})$.
- Activates `binary_sensor.deye_battery_calibration_active`.
- Allows LiFePO4 cells to reach absorption voltage (56.8V – 57.6V) and balance passive shunt bleeders. Automatically cleared after the charging window ends.

---

## 5. Security & 2-Step Confirmation Process

### 5.1 Parameter Classification

| Parameter | Type | Control in Home Assistant | Confirmation Required |
|:---|:---:|:---:|:---:|
| Total Battery Capacity (`batt_capacity`) | Hardware Spec | Read-Only (`sensor.powerbank_total_capacity_kwh`) | N/A (writes blocked) |
| Nominal Voltage (`nominal_voltage`) | Hardware Spec | Read-Only (`sensor.powerbank_voltage_v`) | N/A (writes blocked) |
| Max Charge Current (`max_charge_current`) | Hardware Spec | Read-Only (`sensor.deye_inverter_max_charge_current`) | N/A (writes blocked) |
| Max Discharge Current (`max_discharge_current`) | Hardware Spec | Read-Only (`sensor.deye_inverter_max_discharge_current`) | N/A (writes blocked) |
| Max Charge Level (`deye_battery_max_charge_soc`) | TOU Dispatch | Active Slider (50–100%, default 90%) | No (safe operational dispatch) |
| Morning Reserve Margin (`deye_morning_reserve_margin_pct`) | TOU Dispatch | Active Slider (0–30%, default 10%) | No (safe operational dispatch) |
| Min Discharge SOC (`deye_battery_min_discharge_soc`) | Safety Limit | Active Slider (10–50%, default 20%) | **YES (2-Step Process)** |
| Shutdown SOC (`deye_battery_shutdown_soc`) | Safety Limit | Active Slider (0–30%, default 5%) | **YES (2-Step Process)** |

### 5.2 2-Step Confirmation Flow
1. When user adjusts `min_discharge_soc` or `shutdown_soc` in the UI, `automation.deye_battery_critical_parameters_changed` sets `binary_sensor.deye_battery_critical_parameters_pending` to `on`.
2. Automatic synchronization is paused while pending.
3. User must either:
   - Click **"Zatwierdź i Zastosuj"** (`script.confirm_and_apply_deye_battery_settings`), triggering a confirmation dialog before syncing to Deye.
   - Click **"Cofnij do Falownika"** (`script.revert_deye_battery_settings_from_inverter`), reverting the sliders to live inverter values.

---

## 6. Execution Flow & Automation Schedule

```
[00:00:30 Daily / 12:05 Price Pub / Minute 2 Hourly]
                        │
                        ▼
       automation.deye_tou_hourly_recalculation
                        │
                        ▼
       script.sync_deye_inverter_tou
                        │
                        ▼
       shell_command.sync_deye_inverter_tou
                        │
                        ▼
           config/pstryk_engine.py --sync-deye-tou
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
1. Query Local Solarman V5       2. Deye Cloud OpenAPI
   (TCP 8899 Socket)                (Fallback on timeout)
        │                               │
        └───────────────┬───────────────┘
                        ▼
           Write-on-Change Cache Check
                        │
       ┌────────────────┴────────────────┐
       ▼                                 ▼
   Identical to Inverter?            Changed Schedule?
   [UNCHANGED -> SKIP WRITE]         [WRITE 6 TOU SLOTS]
```
