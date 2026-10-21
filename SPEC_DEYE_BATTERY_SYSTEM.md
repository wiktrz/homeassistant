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

### 1.2 Hardware Register Safety & Control Policy
- **Dynamic Register 108 Modulation:** Modbus Holding Register `108` (`REG_MAX_CHARGE_CURRENT`) is written by `pstryk_engine.py` (`check_and_apply_charge_current_modulation`) to control charging speed. The base is always the 100A ceiling (hard cap `min(100, env)`), never the live register value:
  * Inside a grid-charge window: $I_{\text{target}} = \max(20\text{A}, \operatorname{round}(I_{\text{base}} \times (2 / T)))$ (e.g. 50A for a 4h window, 67A for 3h, 100A for 2h).
  * Outside a window, 05:00-19:00 (`REG108_DAYTIME_START_HOUR`..`REG108_EVENING_PRESET_HOUR`): 100A (PV charges at full rate).
  * After today's window and before 19:00: 100A; from 19:00 the next cycle's target is pre-set.
  * Between 00:00 and the start of today's window: today's window target.
  * `--restore-charge-current` restores 100A. Writes are idempotent (only when the value differs), minimizing EEPROM cycles.
- **Registers 109 and 112 Never Written:** Reg `109` (Max Discharge Current, 140A) and Reg `112` (Max Charge SOC; the HA slider `input_number.deye_battery_max_charge_soc` is the source of truth) are never written by software.
- **Registers 110 and 111 Confirm-Only:** Reg `110` (Min Discharge SOC) and Reg `111` (Shutdown SOC) are written only via the 2-step Confirm button (`script.confirm_and_apply_deye_battery_settings`).

Home Assistant otherwise ingests all parameters in **Read-Only** mode via `sensor.deye_inverter_status`; the hardware limits themselves are configured on the inverter LCD.
### 1.3 Permitted Write Operations
The only write operations permitted from Home Assistant / CLI are:
1. **6-slot Time-of-Use (TOU) Schedule** (Modbus Holding Registers `248..273` or Deye Cloud OpenAPI `/v1.0/order/sys/tou/update`).
2. **Dynamic Charging Current Modulation** (Modbus Holding Register `108` / `/order/battery/parameter/update`) within safe bounds [20A, 100A].

### 1.4 Single Source of Truth & Pre-Check Protocol (Rule 10)
- **Centralized Fallback Storage:** Fallback values (e.g. 16.13 kWh capacity, 51.2 V nominal voltage, 100.0 A max charge current, 140.0 A max discharge current, default IP `10.20.2.6`, port `8899`) reside in **EXACTLY ONE PLACE**: the environment configuration template (`.env.example` / `.env`).
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
| `108` | `0x006C` | Max Charge Current | A (integer) | Read / Write (engine modulation only, 100A ceiling) |
| `109` | `0x006D` | Max Discharge Current | A (integer) | Read-Only |
| `110` | `0x006E` | Min Discharge SOC (Grid Cutoff) | % (integer) | Read / Write (Confirm button only) |
| `111` | `0x006F` | Shutdown SOC (Blackout Cutoff) | % (integer) | Read / Write (Confirm button only) |
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

### 4.1 Role-Based TOU Slots
`generate_deye_tou_schedule()` always emits exactly 6 slots with strictly increasing, minute-based start times in [00:00, 24:00). Each slot has a role: `charge` (grid-charge window, Max SOC, `grid_charge = True`), `peak` (discharge boundary: sell window, morning/evening peak, Min SOC), `reserve` (branch-defined reserve) or `other`. Labels are display-only. The window placement (night, midday, evening/late charge; morning/evening sell) selects the branch; breakpoints are then finalized into 6 slots (missing slots split the longest gap, surplus reserve/other slots with the shortest span are merged).

**Stretch rule** for `reserve`/`other` slots, based on the nearest charge/peak before and after:
- between a charge and the next peak: hold Max SOC;
- between a peak and the next charge: Min SOC;
- between two peaks: keep the branch reserve (morning reserve $\min(\text{max\_soc}, \text{min\_soc} + \text{margin})$, default +10%, or pre-peak $\min(\text{max\_soc}, \text{min\_soc} + \text{pre\_peak\_margin})$).
- "Previous" does not wrap past 00:00; "next" may wrap to today's first events.

**Dynamic Peak & Charging Placement (Zero Hardcoded Boundaries, Strictly No-PV):**
The installation is strictly a No-PV battery storage system. There are no fixed clock boundaries (`06:00`, `10:00`, `12:00`, `17:00`) or "PV refill" heuristics. All slot start times and durations adapt dynamically to market prices:
- **Discharge Peak Ranking:** Driven strictly by gross retail buy price (`price_gross`), NOT prosumer sell price (`price_prosumer_gross`), because zero-export self-consumption (`enableSell: false`) is enforced.
- **Dynamic Dual Peaks:** When 2 distinct price peaks are separated by $\ge 2$ hours, Peak 1 discharges down to `pre_peak_soc`, the intervening valley holds reserve on grid power, and Peak 2 discharges fully down to `safe_min_soc`.
- **Dynamic Charging Duration:** Derived dynamically by `determine_dynamic_charge_hours` from current SOC, target SOC, and pack capacity without hardcoded weekday limits.

Reference schedules (min 10, max 90, margins 10; `G` = grid charge):

| Charge / Peak window | Schedule |
|:---|:---|
| 13-15 / 06-09 | `00:00 20 \| 06:00 10 \| 09:00 10 \| 13:00 90G \| 15:00 90 \| 17:00 10` |
| 13-15 / 17-20 | `00:00 20 \| 06:00 10 \| 13:00 90G \| 15:00 90 \| 17:00 10 \| 20:00 10` |
| 19-21 / 17-19 | `00:00 90 \| 08:00 20 \| 12:00 20 \| 17:00 10 \| 19:00 90G \| 21:00 90` |

**Price coverage:** TOU is neither written nor cached unless the Europe/Warsaw day is covered (`warsaw_day_covered()`: first frame at/before 00:00, last end at/after the next 00:00, at most 1h (`PRICE_COVERAGE_MAX_MISSING_HOURS`) missing in total; handles DST days returned as 24 frames). The cache stores `_warsaw_date` and target hours and is reused only on a match.

**Inverter I/O:** the local Modbus 25s budget starts at the first failed attempt (a healthy run is never cut short); if it is already active, local TOU writes are skipped and the cloud is used (at most once, `cloud_attempted`); the sync aborts on the first failed register write; cloud slots carry the nominal voltage. Read-back labels (`tou_slot_label`) are relative to min/max SOC (±`TOU_LABEL_SOC_TOLERANCE`): `Najtańsze Ładowanie` / `Czuwanie Przed Szczytem (Hold)` / `Szczyt Cenowy (Autokonsumpcja)` / `Rezerwa Przed Szczytem`, fallback thresholds 30/80.

### 4.2 TOU Power Column Semantics & Register 108 Dynamic Modulation
1. **TOU Power is Strictly Discharge Power:**
   In Deye hybrid inverters (SUN-12K-SG05LP3), the `Power(W)` column in the TOU table (Registers 256–261) strictly defines the **maximum discharge power allowed from battery to household loads/grid**. It does **NOT** limit grid charging speed.
   - Throttling `power_w` in TOU slots during daytime or night would throttle battery coverage for household loads, causing unnecessary peak grid imports.
   - Therefore, **all 6 TOU slots maintain full maximum discharge power ($P_{\text{base}}$)**:
     $$P_{\text{base}} = \min(12000\text{ W}, \max(1000\text{ W}, \operatorname{round}(I_{\text{max\_discharge}} \times V_{\text{nominal}}, -1)))$$
     For the physical inverter setting $I_{\text{max\_discharge}} = 140\text{A}$ and $V_{\text{nominal}} = 51.2\text{V}$, $P_{\text{base}} \approx \mathbf{7170\text{ W}}$.

2. **Charging Current Modulation via Register 108 (`Max A Charge`):**
   Charging speed when grid charging is active is governed by Modbus Holding Register 108 (and OpenAPI `/order/battery/parameter/update`):
   - When charging window duration $T$ extends beyond 2 hours (e.g. 4h on weekends), charging current is modulated down to distribute charging across the entire low-price window:
     $$I_{\text{target}} = \max\left(20\text{ A}, \min\left(I_{\text{base}}, \operatorname{round}\left(I_{\text{base}} \times \frac{2}{T}\right)\right)\right)$$
     (e.g., $100\text{A} \times (2 / 4) = \mathbf{50\text{ A}}$ for a 4h window; $100\text{A} \times (2 / 3) = \mathbf{67\text{ A}}$ for a 3h window; $100\text{A}$ for a 2h window).
   - `check_and_apply_charge_current_modulation()` applies $I_{\text{target}}$ inside the window, keeps $I_{\text{base}}$ (100A) during 05:00-19:00 outside the window, and from 19:00 (after today's window) pre-sets the next cycle's target; between 00:00 and the window start it holds today's window target.
   - Writes are strictly idempotent (executed only when the active register value differs from target), protecting EEPROM memory.
3. **Peak Slot Power:** peak slots stay at full base power $P_{\text{base}}$ (7170W) down to `min_soc`.

### 4.3 BMS Calibration Routine (100% Equalization)
Triggered via `script.start_deye_battery_calibration` (flag `input_boolean.deye_battery_calibration_in_progress`):
- The engine (`calibration_active`) raises the Max SOC target of the charge/hold slots to `100%` and does not scale the charge current.
- Allows LiFePO4 cells to reach absorption voltage and balance passive shunt bleeders.
- When `sensor.deye_battery_soc` > 99.5, `script.finish_deye_battery_calibration` clears the flag, stores the calibration date and re-syncs the TOU table with the normal Max SOC target. Reg 112 is never written.

---

## 5. Security & 2-Step Confirmation Process

### 5.1 Parameter Classification

| Parameter | Type | Control in Home Assistant | Confirmation Required |
|:---|:---:|:---:|:---:|
| Total Battery Capacity (`batt_capacity`) | Hardware Spec | Read-Only (`sensor.powerbank_total_capacity_kwh`) | N/A (writes blocked) |
| Nominal Voltage (`nominal_voltage`) | Hardware Spec | Read-Only (`sensor.powerbank_voltage_v`) | N/A (writes blocked) |
| Max Charge Current (`max_charge_current`) | Hardware Spec | Read-Only sensor (`sensor.deye_inverter_max_charge_current`); Reg 108 written only by engine modulation (100A ceiling) | N/A (not user-editable) |
| Max Discharge Current (`max_discharge_current`) | Hardware Spec | Read-Only (`sensor.deye_inverter_max_discharge_current`) | N/A (writes blocked) |
| Max Charge Level (`deye_battery_max_charge_soc`) | TOU Dispatch | Active Slider (50–100%, default 90%) | No (safe operational dispatch) |
| Morning Reserve Margin (`deye_morning_reserve_margin_pct`) | TOU Dispatch | Active Slider (0–30%, default 10%) | No (safe operational dispatch) |
| Min Discharge SOC (`deye_battery_min_discharge_soc`) | Safety Limit | Active Slider (10–50%, default 20%) | **YES (2-Step Process)** |
| Shutdown SOC (`deye_battery_shutdown_soc`) | Safety Limit | Active Slider (0–30%, default 5%) | **YES (2-Step Process)** |

### 5.2 2-Step Confirmation Flow & Execution Sequence
1. When user adjusts `min_discharge_soc` or `shutdown_soc` in the UI, `binary_sensor.deye_battery_critical_parameters_pending` detects mismatch against live inverter readings and turns `on`.
2. Automatic background synchronization continues using active inverter thresholds while critical safety writes remain pending user approval.
3. User must either:
   - Click **"Zatwierdź i Zastosuj"** (`script.confirm_and_apply_deye_battery_settings`), which executes:
     1. `shell_command.apply_deye_battery_safety_limits`: invokes `solarman_v5_client.apply_confirmed_battery_safety_limits` via `pstryk_engine.py --apply-battery-safety-limits`.
        - Writes Register 110 (Min SOC / Batt LOW) and Register 111 (Shutdown SOC) via Modbus RTU (Function 0x06) with socket retry backoff.
        - Falls back to Deye Cloud OpenAPI (`POST /v1.0/order/battery/parameter/update` with `paramterType: "BATT_LOW"`, and `POST /v1.0/order/customControl` for Shutdown SOC) if local TCP socket is refused or timed out.
        - Immediately updates local cache (`/config/.deye_inverter_cache.json`) via cache write-through.
     2. `script.sync_deye_inverter_tou`: calculates and uploads the 6-slot TOU schedule aligned with the new Min SOC.
     3. `homeassistant.update_entity(sensor.deye_inverter_status)`: immediately ingests the updated cache, synchronizing `sensor.deye_inverter_min_discharge_soc` and `sensor.deye_inverter_shutdown_soc` with the sliders, clearing `binary_sensor.deye_battery_critical_parameters_pending` to `off`.
     4. `persistent_notification.create`: notifies the user with confirmation of the new thresholds.
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

---

## 8. Generic UI Pending Operations & Dashboard Blocking Architecture

### 8.1 Problem Statement & Latency Optimization
Writing safety parameters or TOU tables to the inverter involves local Modbus RTU sockets and/or Deye Cloud OpenAPI network requests (~5–10 seconds). Previously:
- The UI allowed concurrent slider adjustments and duplicate button clicks during active execution.
- No visual in-progress indicator was shown, leading to user confusion or accidental re-triggering.
- The pipeline contained a redundant 10-second wait and duplicate `update_entity` calls, inflating latency to ~35s.

### 8.2 Latency Optimizations
1. **Fast-Fail on Connection Refusal:** In `solarman_v5_client.send_command()`, if `ConnectionRefusedError` is caught (port 8899 closed/held by integration), the loop immediately breaks instead of retrying 3 times with 0.5s delays, immediately falling back to Deye Cloud OpenAPI.
2. **Reduced Settle Delay:** `sync_deye_inverter_tou` settle delay reduced from 10s to 2s.
3. **Eliminated Duplicate Entity Updates:** Removed redundant second `homeassistant.update_entity(sensor.deye_inverter_status)` from `confirm_and_apply_deye_battery_settings`.

### 8.3 Generic Pending Operations Helper & Scripts
- **Single Central Helper:** `input_text.pending_ui_operations` (default: `"[]"`). Stores a JSON array of strings representing active operation IDs (e.g. `["deye_battery_safety"]`, `["deye_tou_sync"]`).
- **Generic Management Scripts:**
  - `script.set_ui_operation_pending(operation: string)`: Appends operation ID to JSON list if not already present.
  - `script.clear_ui_operation_pending(operation: string)`: Removes operation ID from JSON list.
  - `script.clear_all_pending_ui_operations`: Emergency reset clearing the list back to `"[]"`.
- **Sensors:**
  - `binary_sensor.ui_any_operation_pending`: Active whenever `input_text.pending_ui_operations` is non-empty.
  - `binary_sensor.ui_op_pending_deye_battery_safety`: Active when `'deye_battery_safety'` is in the active list.
  - `binary_sensor.ui_op_pending_deye_tou_sync`: Active when `'deye_tou_sync'` is in the active list.
- **Auto-Clear Safety Automation:** `auto_clear_stale_pending_ui_operations` in `config/automations.yaml` clears all pending operations if `ui_any_operation_pending` remains active for > 90 seconds, protecting against permanently locked UI states.
- **Dashboard Lovelace Integration:** In `dashboard_modern_reference.yaml` and `dashboard_home_improved.yaml`:
  - When `ui_op_pending_deye_battery_safety` is `on`: Renders a prominent in-progress card (`⏳ Trwa Zapisywanie Ustawień do Falownika Deye...`), hides action buttons, and locks parameter sliders.
  - When `ui_op_pending_deye_battery_safety` is `off`: Standard interactive UI is restored.

