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
- **Dynamic Register 108 Modulation:** Modbus Holding Register `108` (`REG_MAX_CHARGE_CURRENT`) is written by `pstryk_engine.py` (`check_and_apply_charge_current_modulation`). The base is always the 100A ceiling (hard cap `min(100, env)`), never the live register value:
  * Inside a grid-charge slot: the 100A ceiling (the planner always plans the ceiling; a partial hour is shortened in duration instead of reduced in current).
  * Outside every charge slot: the 100A ceiling (Rule 12, no pre-set).
  * `--restore-charge-current` restores 100A. Writes are idempotent (only when the value differs), minimizing EEPROM cycles.
- **Registers 109 and 112 Never Written:** Reg `109` (Max Discharge Current, 140A) and Reg `112` (Max Charge SOC; the HA slider `input_number.deye_battery_max_charge_soc` is the source of truth) are never written by software.
- **Registers 110 and 111 Confirm-Only:** Reg `110` (Min Discharge SOC) and Reg `111` (Shutdown SOC) are written only via the 2-step Confirm button (`script.confirm_and_apply_deye_battery_settings`).

Home Assistant otherwise ingests all parameters in **Read-Only** mode via `sensor.deye_inverter_status`; the hardware limits themselves are configured on the inverter LCD.
### 1.3 Permitted Write Operations
The only write operations permitted from Home Assistant / CLI are:
1. **6-slot Time-of-Use (TOU) Schedule** (Modbus Holding Registers `248..273` or Deye Cloud OpenAPI `/v1.0/order/sys/tou/update`).
2. **Dynamic Charging Current Modulation** (Modbus Holding Register `108` / `/order/battery/parameter/update`) within safe bounds [20A, 100A].

### 1.4 Single Source of Truth & Pre-Check Protocol (Rule 10)
- **Centralized Fallback Storage:** Fallback values (e.g. 16.13 kWh capacity, 51.2 V nominal voltage, 100.0 A max charge current, 140.0 A max discharge current, default IP `10.20.2.6`, port `8899`) reside in **EXACTLY ONE PLACE**: the environment configuration template (`config/.env.example` / `.env`).
- **Zero Hardcoded Literals in Python & YAML:** Python modules (`config/energy_defaults.py`, `config/pstryk_engine.py`, `config/solarman_v5_client.py`, `config/deye_cloud_client.py`) and YAML configurations must NEVER declare inline numeric fallback literals. All code must import and resolve defaults through `config/energy_defaults.py`, which dynamically parses `.env` / `config/.env.example`.
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

### 4.1 Rolling TOU Plan (`plan_rolling_tou`)
There is no separate today/tomorrow schedule. `plan_rolling_tou()` builds **one rolling plan** from *now* to the last published price hour (up to ~48 h) and returns one merged `plan_timeline`: entries `{start, end, role, target_soc, soc_start, soc_end, charge_a, price}` with roles `charge`, `peak`, `hold` or `drain`. The timeline is what the dashboard shows ("Plan Baterii") and what Reg 108 follows.

**Hardware constraint:** the Deye TOU table is 6 clock-keyed slots that repeat every day. The inverter therefore receives `deye_tou_schedule` = the 24 h starting from the behaviour running now. The hourly re-sync rewrites that table only when it changes (`are_tou_slots_identical`). Beyond the last published price the table repeats today's pattern.

**Planning rules:**
- **Peaks per price day:** `analyse_day_price_profile` runs on each Europe/Warsaw day's own prices, so each day's peak is judged against that day's mean gross retail price (Rule 12: `price_gross`, never prosumer price).
- **Grid charge:** greedy cheapest-hour charging (`plan_charge_hours`, below). Calibration or a missing load profile fills to the top in the cheapest hours instead. Explicit `charge_windows` (legacy explicit-window path, `generate_deye_tou_schedule`) bypass the planner.
- **Floors:** one SOC walk (`project_soc_timeline`, Deye floor semantics) over the whole horizon; see *Floors* below.
- **Powerbank window (informational):** `find_powerbank_best_window` sizes N = max(`CHARGE_WINDOW_MIN_HOURS`, energy need at the Reg 108 ceiling) for the sensors and the 22:00 notification, and for the explicit-window path, which sizes the slot charge current from its length (`charge_current_for_window`). Reg 108 is written only by the engine's `check_and_apply_charge_current_modulation` (slot current inside charge slots, ceiling outside); no HA automation lowers it. It does not drive the automatic grid charge plan.

**Dynamic Peak & Charging Placement (Zero Hardcoded Boundaries, Strictly No-PV):**
The installation is strictly a No-PV battery storage system. There are no fixed clock boundaries (`06:00`, `10:00`, `12:00`, `17:00`) or "PV refill" heuristics. All slot start times and durations adapt to market prices:
- **Discharge Peak Ranking:** Driven strictly by gross retail buy price (`price_gross`), NOT prosumer sell price (`price_prosumer_gross`), because zero-export self-consumption (`enableSell: false`) is enforced.

**Greedy Cheapest-Hour Charging (`plan_charge_hours`):**
- Worth covering: price > cheapest candidate × (1 + loss%).
- Candidates: priced hours from now to the last published price, in ascending price order. Each candidate stores only what the shortfall it serves needs: min(ceiling energy left in the hour, room to Max SOC, the shortfall served up to the next hour at most as cheap). A chosen hour grows; a new hour must store and serve at least one `TOU_FLOOR_SOC_STEP` (no trickle). The loop stops when no coverable shortfall remains.
- Need-sized duration (minute-granular): the block runs at the Reg 108 ceiling for ceil(minutes / `TOU_SLOT_MINUTE_STEP`, 5 min) steps; placement is contiguous with an adjacent chosen charge hour, else at the end of its hour; a running hour charges from now. A grid-charge block already on the inverter is kept while its need stays within one step (anti-flap).
- Load: the real Deye profile.
- Example (live prices 23.10, real profile, SOC 17 at 12:18): Fri 13:00–14:00, Sat 03:25–04:00, Sat 12:00–13:30, all at 100 A.
- Beyond the published prices (`estimate_beyond_horizon`, OFF by default): one estimated day priced from the last published day's clock hours. Estimated hours only create shortfalls; they are never charge candidates and never enter the written table, because the daily table is a clock circle and estimate-only charges would merge with today's pattern.
- **Terminal fill:** after the shortfall charges (which keep priority), one more demand "Max SOC at the last published hour" (prices beyond it are unknown) is served by the same cheapest-first step (100 A ceiling, `TOU_SLOT_MINUTE_STEP` blocks, room to Max SOC from that hour on, one `TOU_FLOOR_SOC_STEP` minimum for a new hour, held blocks kept), but only by published hours whose price × (1 + loss%) ≤ the mean gross price of their own Warsaw day (`analyse_day_price_profile`); `terminal_fill=False` gives the need-sized plan. `plan_timeline` charge entries show the real block minutes (rest of the hour = a separate hold entry) and `target_soc` = the projected end SOC; the 6-slot table keeps Max SOC. The AI prompt (`ai_tou_planner.build_prompt`) carries the same rule.
- Removed: reduced-current partial hours (the old window-length current formula) and any stored-energy cost gating (never shipped).

**Floors:**
- Charge hours: grid charge to Max SOC (exact).
- Worth-covering hours: Min SOC (the battery serves the load).
- Other hours: hold at the projected SOC, snapped down to `TOU_FLOOR_SOC_STEP`.
- No load profile or BMS calibration: fill to Max (100 % in calibration) in the cheapest hours, no trickle within one step of the top.
- Explicit windows keep the segment floors of `_assign_cell_floors`.

**Load forecast (real usage history):**
- `deye_cloud_client.fetch_consumption_history()` is read-only: `POST /v1.0/station/list`, then `POST /v1.0/station/history` (granularity 1, 5-minute frames, `startAt`/`endAt`, with a `startDate`/`endDate` fallback). It covers the last `LOAD_HISTORY_DAYS` (.env, default 14) full Warsaw days ending yesterday.
- Cache: `/config/.deye_load_history.json` (override `DEYE_LOAD_HISTORY_FILE`). It is rewritten only when its content changes (flash wear) and refreshed at most once per Warsaw day.
- `build_load_profile()` → weekday and weekend 24-hour profiles in kWh/h (mean W / 1000 per Warsaw local hour, DST-correct; an hour without samples takes the overall mean).
- `load_forecast()` gives the per-hour forecast over the plan; the running hour is blended with the live load (snapped to `LOAD_ESTIMATE_STEP_KW`, .env, default 0.1 kW).
- CLI: `deye_cloud_client.py --fetch-history` (read-only) prints the profile.
- History is fetched ONLY by its own daily job (automation `deye_load_history_daily_fetch` at 04:10 and at HA start -> `shell_command.fetch_deye_load_history` -> `deye_cloud_client.py --fetch-history`); the engine (`load_household_profile`, `rolling_plan_fields`, `fetch_consolidated_data`) reads ONLY the cache file `/config/.deye_load_history.json` and never calls Deye Cloud. Negative cache: a failed fetch writes `{last_attempt, failed: true}` (old frames kept) and retries happen at most every `DEYE_HISTORY_RETRY_HOURS`; the working request variant (`startAt/endAt` vs `startDate/endDate`) is remembered in the cache; days are fetched newest first and the walk stops at the first day failing both variants; `fetched_date` is always stored so same-day reuse works. Status in `input_text.deye_load_history_status` (ok/error). New env var (`config/.env.example` only): `DEYE_HISTORY_RETRY_HOURS`. Stale-sync alert: automation `deye_tou_stale_sync_alert` raises a persistent_notification when `input_datetime.deye_tou_last_synced_date` is older than `input_number.deye_tou_stale_sync_hours` (0 = off; set 36 in the UI, no `initial:`); expected behaviour: when sync stops, the inverter replays the last TOU table daily. The alert is not gated by `input_boolean.deye_tou_auto_sync`; while the switch is off its message says manual mode is active (a switch left off is visible).
- No history: no load forecast; the plan fills to Max in the cheapest hours (see Floors).

**Flash-Wear Hysteresis:** the planner receives the inverter's active slots: held hours (see Greedy), a table anchor at the running inverter slot (the same behaviour is not rewritten each sync), and a tail beyond the published prices copied from the inverter's own slots. `are_tou_slots_identical` compares behaviour at every minute of the daily circle (grid charge exact; other floors within ±`TOU_FLOOR_SOC_STEP`; power within ±`TOU_POWER_DIFF_TOLERANCE_W`), so a boundary that only splits one behaviour is not a difference. In tier-5 charge merges (too many charge blocks) the higher target and current are kept. Non-charge floors snap to `TOU_FLOOR_SOC_STEP` (.env, default 5 %): projected-SOC holds round down, energy needs round up; Min/Max SOC and charge targets are exact.

**Price coverage:** the plan is neither written nor cached unless the Europe/Warsaw day is covered (`warsaw_day_covered()`: first frame at/before 00:00, last end at/after the next 00:00, at most 1h (`PRICE_COVERAGE_MAX_MISSING_HOURS`) missing in total; handles DST days returned as 24 frames). The cache stores `_warsaw_date` and target hours and is reused only on a match.

**Sync triggers:** `sync_deye_inverter_tou_schedule` runs on the daily `00:30` job, on the hourly recalculation, and when tomorrow's prices publish (~12:00).

**Inverter I/O:** the local Modbus 25s budget starts at the first failed attempt (a healthy run is never cut short); if it is already active, local TOU writes are skipped and the cloud is used (at most once, `cloud_attempted`); the sync aborts on the first failed register write; cloud slots carry the nominal voltage. Read-back labels (`tou_slot_label`) are relative to min/max SOC (±`TOU_LABEL_SOC_TOLERANCE`): `Najtańsze Ładowanie` / `Czuwanie Przed Szczytem (Hold)` / `Szczyt Cenowy (Autokonsumpcja)` / `Rezerwa Przed Szczytem`, fallback thresholds 30/80.

`--print-tou` prints the rolling timeline and the 6-slot table (24 h from now) without writing to the inverter.

### 4.2 TOU Power Column Semantics & Register 108 Dynamic Modulation
1. **TOU Power is Strictly Discharge Power:**
   In Deye hybrid inverters (SUN-12K-SG05LP3), the `Power(W)` column in the TOU table (Registers 256–261) strictly defines the **maximum discharge power allowed from battery to household loads/grid**. It does **NOT** limit grid charging speed.
   - Throttling `power_w` in TOU slots during daytime or night would throttle battery coverage for household loads, causing unnecessary peak grid imports.
   - Therefore, **all 6 TOU slots maintain full maximum discharge power ($P_{\text{base}}$)**:
     $$P_{\text{base}} = \min(12000\text{ W}, \max(1000\text{ W}, \operatorname{round}(I_{\text{max\_discharge}} \times V_{\text{nominal}}, -1)))$$
     For the physical inverter setting $I_{\text{max\_discharge}} = 140\text{A}$ and $V_{\text{nominal}} = 51.2\text{V}$, $P_{\text{base}} \approx \mathbf{7170\text{ W}}$.

2. **Charging Current via Register 108 (`Max A Charge`):** every grid-charge slot runs at the 100A ceiling (`BATTERY_CHARGE_CURRENT_MAX_CEILING_A`, hard cap `min(100, env)`). Partial hours are handled by duration (need-sized slots, see Greedy above), not by current. Reg 108 is therefore the ceiling inside and outside charge slots.
   - `check_and_apply_charge_current_modulation()` writes the ceiling, and only when the active register value differs (idempotent, EEPROM protection). `--restore-charge-current` restores 100A.
   - Reduced-current charging (current scaled by window length) is no longer used.
3. **Peak Slot Power:** peak slots stay at full base power $P_{\text{base}}$ (7170W) down to `min_soc`.

### 4.3 BMS Calibration Routine (100% Equalization)
Triggered via `script.start_deye_battery_calibration` (flag `input_boolean.deye_battery_calibration_in_progress`):
- The engine (`calibration_active`) raises the Max SOC target of the charge/hold slots to `100%` and does not scale the charge current.
- Allows LiFePO4 cells to reach absorption voltage and balance passive shunt bleeders.
- When `sensor.deye_battery_soc` > 99.5, `script.finish_deye_battery_calibration` clears the flag, stores the calibration date and re-syncs the TOU table with the normal Max SOC target. Reg 112 is never written.

---

### 4.4 AI-First TOU Planner (`config/ai_tou_planner.py`)
The 6-slot TOU table is proposed first by an LLM (default `MiniMax-M3.1-Flash`, OpenAI-compatible `/chat/completions`, Bearer auth). The deterministic `plan_rolling_tou()` is always computed as the fallback and as the reference the AI must beat. `plan_tou()` in `pstryk_engine.py` is the single wrapper used by the sync path and the `--print-tou` path.

- **Prompt:** system rules (zero export, no PV, 6 clock-keyed slots covering 24 h, cheapest hours first, peaks per price day, battery-health rules) plus a JSON payload: Warsaw time, hourly `price` (gross retail) and `load_kwh` from `load_forecast()` over real Deye history, battery state (SOC, Min/Max SOC, capacity, voltage, ceiling A, loss %), table limits (`TOU_SLOT_COUNT`, `TOU_SLOT_MINUTE_STEP`, `TOU_FLOOR_SOC_STEP`), `active_slots` and the algorithm's own table and timeline. All numbers come from live data or `.env.example`; the prompt never contains the key.
- **Request:** `AI_TOU_BASE_URL` + `/chat/completions`, `AI_TOU_MODEL`, `max_completion_tokens` = `AI_TOU_MAX_TOKENS`, `reasoning_effort` = `AI_TOU_REASONING_EFFORT`, wall-clock deadline `AI_TOU_TIMEOUT_S` (default 15 s; the HTTP call runs in a daemon thread joined with that timeout). The call is skipped (`fallback_reason = ai_skipped_budget`) when elapsed sync time + `AI_TOU_TIMEOUT_S` + `LOCAL_TIME_BUDGET_S` exceeds `SHELL_COMMAND_TIMEOUT_S` (HA shell_command hard kill, 60 s). A non-https `AI_TOU_BASE_URL` is refused (`ai_insecure_base_url`). No `temperature`, `response_format` or `thinking` flags (M3.1-Flash always thinks; `thinking: disabled` is a 400). Empty `AI_TOU_API_KEY` disables the AI entirely.
- **Parsing:** only `message.content` is read (never `reasoning_content`); `<think>` blocks and code fences are stripped and the first JSON object is parsed.
- **Validation (`validate_ai_slots`):** exactly `TOU_SLOT_COUNT` slots; `HH:MM` on the `TOU_SLOT_MINUTE_STEP` grid, strictly increasing; `grid_charge` is bool; `min_soc <= target_soc <= max_soc`; `0 <= charge_current_a <= ceiling`; `power_w` within inverter bounds. Valid answers are turned into slots by the existing `_tou_table_slots` builder, so labels and power columns match the algorithm.
- **Cost and safety guard (`evaluate_ai_slots`):** the AI table is simulated with the same SOC walk over the same load forecast. It is rejected if projected SOC breaks Min SOC in an hour the algorithm covers, or if simulated grid cost exceeds the algorithm's by more than `AI_TOU_MAX_COST_RATIO`.
- **Fallback:** any failure (`ai_disabled`, `ai_call_failed` incl. timeout/HTTP, `ai_truncated` on `finish_reason=length`, `ai_empty_answer`, `ai_bad_json`, validation or guard rejection) yields `plan_source = "algorithm"` with a `fallback_reason`. The key is never logged or returned.
- **When it is called:** only on the sync path (`sync_deye_inverter_tou_schedule`) and explicit CLI. A call happens when the price fingerprint changed (SHA-256 of published hour starts and retail gross prices), or on deviation (actual SOC < planned `soc_end` - `AI_TOU_DEVIATION_SOC`), with at most one deviation call per `AI_TOU_MIN_INTERVAL_MIN`. `--sync-deye-tou --force` forces a call.
- **Cache:** `AI_TOU_CACHE_FILE` (default `/config/.deye_ai_tou_plan.json`), written only when the content changes (flash wear), reused only while it matches the current fingerprint and still covers now. Failures are cached as negative results until the next fingerprint change or interval, so they do not retry every hour. The sensor path (`rolling_plan_fields`) reads this cache only and never opens a socket; it exposes `plan_source`, `ai_last_call` and `fallback_reason`.
- **Manual TOU (`sync_manual_tou_schedule`):** used only while `input_boolean.deye_tou_auto_sync` is off. The 6 user slots are checked with bounds-only validation (no cost guard), built with `_tou_table_slots`, charged at the ceiling (no Reg 108 modulation) and written through the existing `sync_tou_schedule` (cloud fallback). `--dry-run` validates and prints without writing. A real write needs `--auto-sync-off` (rendered by `shell_command.sync_deye_manual_tou` only while the switch is off), refuses when inverter telemetry reports no battery, and is skipped when `are_tou_slots_identical` with the active table unless `--force`. Malformed slot fields always give a clean rejection (no `power_w` field is read).
- **Unchanged:** `are_tou_slots_identical`, the write path, Reg 108 = ceiling policy, Reg 109/112 never written.

## 5. Security & 2-Step Confirmation Process

### 5.1 Parameter Classification

| Parameter | Type | Control in Home Assistant | Confirmation Required |
|:---|:---:|:---:|:---:|
| Total Battery Capacity (`batt_capacity`) | Hardware Spec | Read-Only (`sensor.powerbank_total_capacity_kwh`) | N/A (writes blocked) |
| Nominal Voltage (`nominal_voltage`) | Hardware Spec | Read-Only (`sensor.powerbank_voltage_v`) | N/A (writes blocked) |
| Max Charge Current (`max_charge_current`) | Hardware Spec | Read-Only sensor (`sensor.deye_inverter_max_charge_current`); Reg 108 written only by engine modulation (100A ceiling) | N/A (not user-editable) |
| Max Discharge Current (`max_discharge_current`) | Hardware Spec | Read-Only (`sensor.deye_inverter_max_discharge_current`) | N/A (writes blocked) |
| Max Charge Level (`deye_battery_max_charge_soc`) | TOU Dispatch | Active Slider (50–100%, default 90%) | No (safe operational dispatch) |
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
- **Dashboard Lovelace Integration:** In `dashboard_modern_reference.yaml`:
  - When `ui_op_pending_deye_battery_safety` is `on`: Renders a prominent in-progress card (`⏳ Trwa Zapisywanie Ustawień do Falownika Deye...`), hides action buttons, and locks parameter sliders.
  - When `ui_op_pending_deye_battery_safety` is `off`: Standard interactive UI is restored.

