# Home Assistant Improvements Research - Future Implementation Guide

*Generated: 2026-04-05*

## Current Setup Analysis
- HA Container (Core 2026.1.3)
- ESP32 devices via BoneIO (MQTT)
- Shelly RGBW2 lights
- 6 heating zones (local01-06) with MQTT thermostats
- Motion sensors (PIR), door sensors (Reed), presence sensors
- MQTT-based automation
- 70+ automations, mostly device-based triggers
- Custom scripts for Shelly animations
- AWS SQS integration
- Google Generative AI for image recognition
- Tesla charging integration
- Pstryk energy price sensor

---

## 1. LLM/AI Integration (HIGH PRIORITY)

### Currently Implemented
- `google_generative_ai_conversation` for camera image recognition
- Detailed architectural proposal & roadmap: [`CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md`](CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md) (Generic Camera AI Engine, face comparison with reference portraits, access control)

### Recommended Additions

**Conversation Agent (Built-in since HA 2024.4+)**
```yaml
conversation:
  agent:
    integration: demo
```
- Natural language control of Home Assistant
- Works with the Assist pipeline

**Local LLM Options (2025-2026)**

| Option | Description | Benefit |
|--------|-------------|---------|
| **Ollama** | Local LLM server | Privacy, no cloud dependency |
| **LlamaCPP** | Native HA integration | Runs models locally |
| **Hugging Face** | Cloud AI with local fallback | Flexible model selection |

### What It Would Enable
- "Turn on heating in bedroom"
- "What's the current temperature in salon?"
- "Show me energy usage today"
- "When is the cheapest energy window today?"
- "Set salon lights to blue"

---

## 2. Energy Dashboard (HIGH PRIORITY) ✅ IMPLEMENTED

You have the perfect setup:
- `sensor.pstryk_price_meter` (energy prices)
- Tesla charging automation
- 6 heating zones

### Energy Dashboard Could:
- Show real-time price vs consumption
- Predict optimal charging windows
- Track daily/weekly costs

### Configuration Needed
```yaml
# Add to configuration.yaml
energy:
  solar_forecast: sensor.pstryk_price_meter
```

### Future Enhancements
- Other high-power devices (water heater, dryer)
- Historical cost tracking
- Automated heating schedule around energy prices

---

## 3. Voice Control via Wyoming Protocol (MEDIUM)

### Wyoming Protocol (Home Assistant 2023.12+)

The Wyoming protocol enables local voice assistants with:
- **Piper** - Local text-to-speech (lightweight, runs on Pi)
- **Whisper** - Local speech-to-text (accurate, privacy-preserving)
- **Sonar** - Wake word detection

### Architecture
```
+------------+     +----------+     +-----------+
| Microphone | --> | Whisper  | --> | HA Assist |
+------------+     +----------+     +-----------+|
                                        |
                                   +---------+
                                   | Piper   |
                                   +---------+
                                        |
                                   +------------+
                                   | Speakers   |
                                   +------------+
```

### For Your Use Case
- Voice control of heating: "Turn up heat in bedroom"
- Query energy prices: "When is cheapest electricity?"
- Control Shelly lights via voice

---

## 4. Dashboard Enhancements (MEDIUM)

### Modern Cards Available

| Card Type | Use Case | File to Modify |
|-----------|----------|----------------|
| `tile` | Richer entity tiles | `dashboard_improved.yaml` |
| `histogram` | Energy price history | `dashboard_improved.yaml` |
| `forecast` | Price predictions | `dashboard_improved.yaml` |
| `picture-glance` | Camera with toggles | `dashboard_improved.yaml` |
| `flex-table` | Heating zones overview | `dashboard_home_improved.yaml` |
| `thermostat-hvac` | 6 heating zones | `dashboard_home_improved.yaml` |
| `light-slider` | Shelly RGBW brightness | `dashboard_improved.yaml` |

### Example: Tile Card
```yaml
type: custom:tile
entity: sensor.local00_kuchnia_temperature
icon: mdi:thermometer
aspect_ratio: 1
color: auto
```

### Example: Flex Table for Heating
```yaml
type: flex-table
entities:
  - entity: sensor.local01_glowny_temperature
  - entity: sensor.local01_heating_state
columns:
  - name: Room
    attr: friendly_name
  - name: Temp
    entity: sensor.local01_glowny_temperature
  - name: Heating
    entity: binary_sensor.local01_heating_state
```

### Example: Energy Price Histogram
```yaml
type: histogram
entity: sensor.pstryk_price_meter
hours_to_show: 48
```

---

## 5. Migrate to State-Based Triggers (LOW-MEDIUM)

### Current State
Your automations use device-based triggers heavily:
```yaml
triggers:
  - type: turned_on
    device_id: d8e2df1faca63578472e0677273680e2
    entity_id: cfd71bd4dfde649c02233583749e9a79
    domain: binary_sensor
    trigger: device
```

### Recommended State-Based Pattern
```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.kitchen_motion
    to: 'on'

conditions:
  - condition: state
    entity_id: input_boolean.motion_lights_disabled
    state: 'off'

actions:
  - action: light.turn_on
    target:
      entity_id: light.kitchen_lights
    data:
      brightness_pct: 25
```

### Benefits
1. Works across HA restarts without re-linking
2. Portable when replacing hardware
3. Easier to debug with entity names

---

## 6. MQTT → Climate Entities (MEDIUM)

### Current Issue
Your heating uses raw MQTT publish:
```yaml
- service: mqtt.publish
  data:
    topic: local01/thermostat/Glowny/routine/set
    payload: '{{ states(''input_select.local01_main_heating_routine'') }}'
```

### Recommended: Climate Entities
```yaml
# In mqtt.yaml - migrate from sensors to climate
climate:
  - unique_id: local01_glowny_thermostat
    name: Local01 Glowny Thermostat
    current_temperature_topic: "local01/Glowny/details"
    current_temperature_template: "{{ value_json.temperature }}"
    temperature_command_topic: "local01/thermostat/Glowny/temperature/set"
    mode_command_topic: "local01/thermostat/Glowny/mode/set"
    modes: ["off", "heat"]
```

### Benefits
- Built-in thermostat UI
- Integration with energy dashboard
- Easier automations

---

## 7. Presence Detection Enhancement

You have RFID tags and motion sensors. Consider adding:
```yaml
device_tracker:
  - platform: nmap
    hosts: 10.20.2.0/24  # Your network
```
This supplements tag-based detection with network-level presence.

---

## 8. ESP32 Improvements

### Critical Issues (from ISSUES.md)
- MEM-001, MEM-002, MEM-003 - Memory safety issues

### Fix Pattern
```cpp
// In main.cpp loop()
if (MQTTA != nullptr) MQTTA->loop();
if (OWM != nullptr) OWM->run();
```

### MQTT Improvements
Instead of direct MQTT publish, use HA entity states.

---

## Priority Summary

| Priority | Action | Impact |
|----------|--------|--------|
| 1 | Energy Dashboard | Cost savings |
| 2 | Migrate device-based triggers to state-based | Maintainability |
| 3 | Implement Assist + Wyoming voice control | Accessibility |
| 4 | Fix ESP32 null pointer issues (MEM-001, MEM-002) | Stability |
| 5 | Add modern dashboard cards (tile, histogram) | UX improvement |
| 6 | Migrate MQTT heating control to climate entities | Standardization |
| 7 | Add Ollama/local LLM for privacy | AI assistance |
| 8 | Presence detection via nmap | Better tracking |
