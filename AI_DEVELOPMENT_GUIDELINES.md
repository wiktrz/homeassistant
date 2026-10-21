# AI Development Guidelines — Home Assistant & Home Automation Ecosystem

> **Scope:** Authoritative development rules, planning gates, documentation synchronization protocols, and safety constraints for all AI agents working within `/Users/wtrzonkowski/Desktop/private/homeassistant` and the linked `/Users/wtrzonkowski/Desktop/private/home_automation` project.

---

## 1. Core Operating Rules for AI Agents

### Rule 1: Strict Planning Mode Gate (Read-Only Gate)
- Whenever in **Planning Mode (`/plan`)**, design mode, exploration, research, review, or preparing plan artifacts, AI agents must remain **strictly read-only**.
- **NEVER** modify, create, or delete workspace files, or execute mutating commands while in planning mode.
- Automated stop-hook approvals (e.g. *"automatically approved through review policy"*) **do NOT** grant permission to execute.
- The agent must pause and await the user's **explicit conversational command** (e.g. *"proceed"*, *"execute"*, *"approved"*) before applying any code, configuration, or file modifications.

### Rule 2: Mandatory Self-Updating Documentation Protocol
- **Every change requires documentation synchronization:** Whenever an AI agent makes changes to code, configuration, YAML definitions, automations, scripts, entities, MQTT topics, or hardware pins, it **MUST automatically update** the corresponding documentation before completing the task.
- **Documentation Update Checklist:**
  1. **[`PLACES_AND_BEHAVIORS.md`](PLACES_AND_BEHAVIORS.md):** If any room/place entity, trigger, condition, action, wall button, script, scene, or sensor behavior is modified or added, update that specific room's section and the quick-lookup matrix.
  2. **[`AGENT.md`](AGENT.md) & [`AGENTS.md`](AGENTS.md):** If system topology, components, hardware mapping (BoneIO relays/dimmers, ESP32 pins), safety constraints, or MQTT bus topics change, update both files in sync.
  3. **[`llms.txt`](llms.txt):** If new architecture files, proposals, dashboards, or core subsystems are added or retired, update the global index and summary.
  4. **[`CLAUDE.md`](CLAUDE.md):** If developer CLI commands, Docker containers, or environment requirements change, update accordingly.

### Rule 3: Git Commit & Push Safety (Strictly Enforced)
- **NEVER stage (`git add`)**, commit (`git commit`), or push (`git push`) changes unless the user explicitly and unambiguously requests it in their message using the exact words `'commit'` or `'push'`.
- Generic requests like *"fix it"*, *"implement"*, *"deploy"*, *"repair"*, or *"save"* do **NOT** grant permission to commit or push.
- All changes must remain in the working directory (**unstaged**) until instructed otherwise.
- **NEVER** run `git push` unless explicitly commanded with the word `'push'`.

### Rule 4: Mandatory Context Initialization
- In this repository, all documentation-related and feature-implementation tasks **must start by reading [`AGENT.md`](AGENT.md)** to establish complete project and edge ecosystem context.
- Consult [`PLACES_AND_BEHAVIORS.md`](PLACES_AND_BEHAVIORS.md) for entity IDs, hardware channel mapping, and existing automation logic before adding or modifying any entity.

### Rule 5: Database & Cache Preservation
- **NEVER clear or delete optimization databases** or task cache tables (e.g. `learning.db`, SQLite cache tables, or conversation histories). Persistent history is a desired requirement.

### Rule 6: Mandatory Automated Test Verification Protocol
- Prior to completing any coding or YAML modification task, the AI agent **MUST run the automated test harness**:
  ```bash
  python3 tests/runner.py --auto
  ```
- Any failed test or broken hardware invariant (Tier 0–Tier 3) blocks completion and must be resolved immediately.
- When new features, scripts, or automations are introduced, the agent must ensure a declarative test scenario is created in `tests/scenarios/` and registered into `tests/test_registry.yaml` to lock in regression protection.

### Rule 7: Mandatory End-of-Session Retrospective & Quality Re-Validation Protocol
- At the conclusion of every substantive development session (or when requested by the user), the AI agent **MUST perform a structured retrospective** and re-validation covering:
  1. **Semantic Domain Logic vs Communication Success:** Never declare completion solely because code executed without syntax errors or an API returned 200/OK. Explicitly verify that the domain logic, calculated values, and real-world conditions (e.g. `Europe/Warsaw` local timestamps vs UTC `Z`, dynamic midday spot tariffs vs hardcoded night assumptions) strictly match the user's intent.
  2. **Hardware & Flags Principle of Least Privilege:** Verify that auxiliary hardware controls (e.g. backup generator charging `enableGeneration` / GEN, grid export `enableSell`, high-voltage relays) are strictly disabled unless explicitly required and physically present.
  3. **Anti-Silence & Continuous Conversational Feedback:** Never end a turn silently after a background task or tool execution. Always provide clear, visible conversational answers to keep the user informed without requiring "try again" prompts.
  4. **Documentation & Invariant Re-Check:** Re-verify that documentation files (`PLACES_AND_BEHAVIORS.md`, `AGENT.md`, `AGENTS.md`, `llms.txt`) and hardware safety invariants (Tier 1A) remain 100% aligned with the codebase.

### Rule 8: Strict English Code & Identifiers Standard (Apart from User-Facing UI)
- **Strict English Code Standard:** All code identifiers, variable names, functions, classes, arguments, YAML keys, Jinja template/namespace variables, Home Assistant `unique_id` attributes, entity IDs, CLI arguments, and schema fields **MUST be strictly in English**.
- **No Mixed Language ("Ponglish" / Hybrid):** Never mix Polish and English within an identifier (e.g. `jutro_date`, `jutro_dni`, `pstryk_best_window_jutro_dol` are strictly prohibited). Use clean English names like `tomorrow_date`, `polish_weekdays`, `tomorrow_weekday`, `pstryk_best_window_tomorrow_dol`.
- **Polish Strictly Reserved for User-Facing UI:** Polish language is permitted and expected **ONLY** in user-facing presentation layers:
  - Dashboard titles, card labels, and column headers (e.g. `title: Aktywne Sloty Falownika Deye`)
  - Friendly entity names when required for voice/UI (`friendly_name:`)
  - Spoken TTS voice output (e.g. Nabu Casa / Voice PE announcements)
  - User notifications (messages and titles sent via `notify.notify` or `persistent_notification`)
  - User-facing status string values meant for display (e.g. `"Oczekiwanie na publikację (ok. 12:00)"`, `"Najtańsze Ładowanie"`)
- Everything under the hood (logic, variables, templates, attributes, internal constants) must be 100% English.

### Rule 9: Zero Hardcoded Hardware Parameters (Battery & Inverters)
- **Zero Hardcoding Rule:** Battery storage and hybrid inverter hardware parameters (total capacity in kWh, nominal voltage in V, maximum charge/discharge current in A, and battery model names) must **NEVER** be hardcoded as static constants or fallback values anywhere in YAML files (templates, configurations, dashboards, scripts, automations) or python engines.
- All hardware specifications must be read dynamically from the physical inverter registers (Modbus reg `102` for Ah, reg `108` for charge current, reg `587` for voltage) or Cloud OpenAPI (`/v1.0/config/battery`), dynamically deriving `batt_capacity_kwh = (batt_capacity * nominal_voltage) / 1000.0`.
- Continuous automated regression protection is enforced by `tests/invariants/test_safety_rules.py::test_no_hardcoded_battery_parameters`.
- Complete hardware specifications, caching rules, and register mappings are defined in [`SPEC_DEYE_BATTERY_SYSTEM.md`](SPEC_DEYE_BATTERY_SYSTEM.md).

### Rule 10: Single Source of Truth & Mandatory Variable Pre-Check Protocol
- **Strict Single Source Invariant:** Fallback values (e.g. `16.13` kWh capacity, `51.2` V nominal voltage, `100.0` A max charge current, default IP `10.20.2.6`, port `8899`) must exist in **EXACTLY ONE PLACE**: the environment configuration template (`.env.example` / `.env`).
- **Zero Fallback Literals in Python & YAML:** Python modules (`config/energy_defaults.py`, `config/pstryk_engine.py`, `config/solarman_v5_client.py`, `config/deye_cloud_client.py`) and YAML definitions must NEVER declare inline numeric fallback literals. All code must import and resolve defaults through `config/energy_defaults.py`, which dynamically parses `.env` / `.env.example`.
- **Mandatory Pre-Check Protocol Before Adding Variables:** Before defining any new constant, environment variable, or Home Assistant helper (`input_*`), AI agents and developers **MUST run the pre-check tool**:
  ```bash
  python3 scripts/check_duplicate_variable.py --search "<concept_name>"
  ```
  If an existing variable, helper, or constant already serves the intended purpose, it **MUST BE REUSED**. Creating duplicate or parallel variables is strictly prohibited.
- **Continuous Invariant Protection:** Enforced continuously by `scripts/check_duplicate_variable.py --validate` and `tests/invariants/test_no_duplicate_defaults.py`.

### Rule 11: Lovelace Dashboard YAML Schema Integrity & Indentation Standards
- **Strict Indentation Under `card:` in Conditional Cards:** When using `type: conditional` cards to wrap inner cards (such as `type: entities`), child card configuration keys (especially `entities:` or `cards:`) must be strictly indented under `card:`. Never place `entities:` or `cards:` at the same indentation level as `card:` (sibling indentation). Sibling indentation produces syntactically valid YAML that passes basic linters but triggers Home Assistant frontend runtime failure: *"Błąd konfiguracji (No entities specified)"*.
- **Mandatory Non-Empty Lists:** Every `type: entities` card must define a non-empty `entities:` list. Every container card (`vertical-stack`, `horizontal-stack`, `grid`) must define a non-empty `cards:` list.
- **Conditional Rows vs Conditional Cards:** Conditional rows inside `type: entities` must define `conditions:` and `row:`, while conditional cards must define `conditions:` and `card:`.
- **Automated Regression Protection:** Continuously enforced by `tests/invariants/test_dashboard_schemas.py` across all dashboard files in `config/dashboards/*.yaml`.

### Rule 12: Agile Market-Driven Dispatch & Zero Hardcoded Heuristics
- **Zero Hardcoded Clock Boundaries:** Battery Time-of-Use (TOU) schedules and inverter modulation must **NEVER** use hardcoded time boundaries (e.g. `06:00`, `10:00`, `12:00`, `17:00`, `05:00`, `19:00`). All slots and charging/discharging durations must be derived dynamically from dynamic market pricing (`Pstryk API`), current SOC, target SOC, and user configuration.
- **Zero Phantom PV Assumptions:** This installation is strictly a **"No-PV"** battery storage system with zero solar panels installed. Code, automations, and documentation must NEVER assume solar generation, "PV refill", or solar-driven daytime charging current modulation.
- **Retail Buy Price Peak Ranking:** In a zero-export system (`enableSell: false`), discharge peak windows must be ranked strictly by retail gross buy price (`price_gross`), NOT by prosumer sell price (`price_prosumer_gross`), to maximize grid purchase avoidance.
- **Register 108 Current Modulation Policy:** Dynamic charging current modulation ($I = \min(100, \max(20, \text{round}(100 \times 2 / T)))$) applies strictly inside the dynamic charging window. Outside all charging windows, register 108 must strictly reset to baseline `BATTERY_CHARGE_CURRENT_MAX_CEILING_A` (100A).
- **Automated Invariant Regression Protection:** Enforced continuously by `tests/invariants/test_no_hardcoded_tou_hours.py`.

---

## 2. Hardware Safety & Architectural Invariants

### Deye Inverter & Battery Storage Protection Invariants
- **Register Write Policy (108..112):** Reg 108 (Max Charge Current) is written only by the engine's charge-current modulation (`set_charge_current` / `--restore-charge-current`), based on the `BATTERY_CHARGE_CURRENT_MAX_CEILING_A` (100A) ceiling and never on the live register value (no ratchet). Regs 109 (Max Discharge Current) and 112 (Max Charge SOC) are **never written** by software. Regs 110 (Min Discharge SOC) and 111 (Shutdown SOC) are written **only** via the 2-step Confirm button. Everything else on the inverter is read-only; the TOU table (regs 248..273) is the only other write.
- **Write-on-Change Caching & Logger Flooding Protection:** All Deye reads implement a 30s TTL cache with semantic write-on-change comparison (`are_deye_readings_identical`). Cache writes to disk occur strictly when data changes to eliminate flash storage wear. On network dropouts, systems fall back to the last known cache to prevent sensor flapping.
- **2-Step Confirmation Process:** Modifications to safety-critical limits (`min_discharge_soc`, `shutdown_soc`) pause automatic synchronization and require explicit confirmation via "Zatwierdź i Zastosuj" (`script.confirm_and_apply_deye_battery_settings`).
- **Dynamic Charging Power Scaling (+10% Loss Buffer):** When charging windows exceed 2 hours (e.g. weekends), power scales dynamically to the energy delta $\Delta SOC = \max(0, \text{max\_soc} - SOC_{\text{current}})$ with an extra 10% buffer for conversion efficiency without exceeding maximum safe hardware power. Peak slots (e.g. evening discharge) strictly remain at full base power $P_{\text{base}}$.

### Protected Power Supplies (Exclusion from All-Off Scripts)
The following 4 BoneIO relay channels power critical LED transformers and ambient controllers:
- `switch.zasilanie_salon_dodatkowe` (`light.boneio_32_l_07_new_light_18`)
- `switch.zasilanie_sypialnia_garderoba` (`light.boneio_32_l_07_new_light_07`)
- `switch.zasilanie_mala_lazienka` (`light.boneio_32_l_07_new_light_20`)
- `switch.zasilanie_taras` (`light.boneio_32_l_07_new_light_11`)

> [!CAUTION]
> **NEVER include these power supplies in `script.all_lights_off`**, night-leaving routines, or broad light groups. Cutting power to these relays disables local wall switches and causes dependent LED controllers to lose network connectivity.

### Electric Strike Lock Safety
- Front door strike lock is connected to BoneIO relay 23 (`lock.rygiel_drzwi_wejsciowych_lock`).
- Any automation or manual button triggering the strike (`input_button.btn_entrance_door`) must include an automatic safety shutoff within **2 minutes** to prevent coil overheating and hardware burnout.

### Home Assistant MQTT Configuration Standard
- **NEVER use `object_id` in `config/mqtt.yaml`**. Home Assistant manual MQTT schema only accepts `unique_id:` and `name:`.
- For Polish entity names, retain standard english MQTT names in `config/mqtt.yaml` and apply Polish labels via `config/customize.yaml` (`friendly_name:`) or via Home Assistant UI entity registry.

### ESP32 Firmware & Protocol Safety
- **Null Safety:** Always check pointers before dereferencing (`if (MQTTA != nullptr) ...`).
- **Non-blocking Execution:** Never introduce `delay()` in firmware loops; use `millis()` timers.
- **OneWire Conversion:** Set `sensors.setWaitForConversion(false)` on DallasTemperature instances.
- **Day Indexing Discrepancy:** C++ `tm_wday` and Node.js `getDay()` use 0=Sunday. Map days using ISO 8601 `(day === 0) ? 7 : day` to prevent schedule shifts.

