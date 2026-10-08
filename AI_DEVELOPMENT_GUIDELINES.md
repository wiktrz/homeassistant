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

---

## 2. Hardware Safety & Architectural Invariants

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
