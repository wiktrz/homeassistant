# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

> **Key Companions:**
> - [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md) — Mandatory AI development rules, planning gates, and documentation update protocols.
> - [`llms.txt`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/llms.txt) — Global LLM & human project index and architecture map.
> - [`PLACES_AND_BEHAVIORS.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/PLACES_AND_BEHAVIORS.md) — Exhaustive room-by-room entity directory and behavioral logic map.
> - [`AGENT.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENT.md) — Unified Home Assistant & ESP32/Node.js ecosystem bridge.

## Core AI Operating Rules

1. **Strict Planning Mode Gate (Read-Only Gate):** When in `/plan`, design mode, or preparing artifacts, AI agents must remain strictly read-only. NEVER modify, create, or delete workspace files without explicit conversational user approval. Automated stop-hook approvals do NOT grant permission to execute.
2. **Mandatory Documentation Self-Update:** Any modification to code, configuration, YAML, automations, scripts, entities, or hardware MUST trigger an automatic update to documentation (`PLACES_AND_BEHAVIORS.md`, `AGENT.md`, `AGENTS.md`, `llms.txt`) before completing the task.
3. **Git Commit & Push Safety:** NEVER run `git add`, `git commit`, or `git push` unless explicitly commanded using the exact words 'commit' or 'push'. Leave all changes unstaged.
4. **Context Rule:** Always start documentation and implementation tasks by reading [`AGENT.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AGENT.md) and [`AI_DEVELOPMENT_GUIDELINES.md`](file:///Users/wtrzonkowski/Desktop/private/homeassistant/AI_DEVELOPMENT_GUIDELINES.md).

## Project Overview

This is a Home Assistant deployment project using Docker for home automation control. It integrates with the `home_automation` project which contains ESP32 heating controllers and Node.js backend.

**Key Components:**
- **Home Assistant** - Main UI, automations, MQTT sensors/climate controls
- **Zigbee2MQTT** - Zigbee device integration (Sonoff Zigbee 3.0 USB Dongle Plus V2)
- **Pstryk Energy Pricing** - Dynamic energy pricing from Polish provider
- **Home Assistant Voice PE** - Voice satellite device (ESPHome, Nabu Casa) for voice assistant and TTS announcements

**Location:** `/Users/wtrzonkowski/Desktop/private/homeassistant/`

## Architecture

### Docker Services

| Service | Image | Network | Purpose |
|---------|-------|---------|---------|
| homeassistant | `ghcr.io/home-assistant/home-assistant:stable` | host | Main HA instance |
| zigbee2mqtt | `ghcr.io/koenkk/zigbee2mqtt` | bridge | Zigbee bridge |

### Network Configuration

| Service | Port | Protocol |
|---------|------|----------|
| Home Assistant | 8123 | HTTP |
| Zigbee2MQTT Frontend | 8124 | HTTP |
| Zigbee2MQTT Serial | /dev/ttyUSB0 | UART |

**Note:** Home Assistant uses `network_mode: host` for direct host network access.

## Configuration Structure

```
config/
├── configuration.yaml     # Main HA config (location, units, includes)
├── mqtt.yaml             # MQTT sensors, lights, climate entities
├── automations.yaml      # 60+ automation rules
├── scripts.yaml          # HA scripts (roller, lighting, cinema)
├── scenes.yaml           # Scene definitions
├── templates.yaml        # Template sensors (Pstryk pricing)
├── customize.yaml        # Entity customization
├── secrets.yaml          # Sensitive configuration
├── views.yaml            # View definitions
├── dashboard_modern_reference.yaml # Dom iPad Modern dashboard (/dashboard-home)
├── dashboard_home_improved.yaml   # Home dashboard
├── dashboard_improved.yaml       # Main dashboard
├── dashboard_korytarze_improved.yaml  # Hallways dashboard
├── blueprints/           # HA blueprint templates
│   ├── automation/
│   │   ├── motion_light.yaml
│   │   ├── notify_leaving_zone.yaml
│   │   └── script/
│   │       └── confirmable_notification.yaml
│   └── template/
│       └── inverted_binary_sensor.yaml
└── pstryk_pricing.py     # Energy pricing script

data/
└── configuration.yaml    # Zigbee2MQTT configuration
```

## MQTT Integration

### MQTT Topics Consumed

**Heating Controller Topics:**
```
heating_controller/{name}/power                    # Power state
heating_controller/{name}/{flat}/{pin}/state     # Pin ON/OFF state
{flat}/thermostat/{pin}/routine                  # Current routine name
{device}/onewire/found                           # Discovered sensors
```

**Thermostat Topics:**
```
local00-{local06}/{Room}/details                 # Temperature/details JSON
local0X/thermostat/{Room}/temperature/set        # Set target temperature
local0X/thermostat/{Room}/routine/set            # Change heating plan
room0X_{room}/sensor/temperature/state           # Temperature readings
```

### Supported Locales

| Locale | Type | Thermostats |
|--------|------|-------------|
| local00 | Primary/Home | Kuchnia, Salon, Sypialnia, Lazienka, MalaLazienka, Klara, Nikola, Gabinet, Gospodarcze, Wiatrolap, HolWejscie, HolSypialnia |
| local01-local06 | Rental Units | Glowny, Lazienka, Sypialnia |

## Zigbee2MQTT Configuration

**Serial Device:** Sonoff Zigbee 3.0 USB Dongle Plus V2
**Adapter:** ember (Silicon Labs EmberZNet)
**Zigbee Channel:** 11
**PAN ID:** 4516

**Key Settings (data/configuration.yaml):**
```yaml
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://localhost:1883
serial:
  adapter: ember
  baudrate: 115200
  rtscts: true
frontend:
  enabled: true
  port: 8124
homeassistant:
  enabled: false  # Using manual MQTT config instead
```

## Automations Overview

### Heating/Routine Automations (18)
- Thermostat routine changes based on input_select for local01-local06

### Lighting Automations & Architecture
- Motion-triggered lights (PIR sensors)
- Time-based dimming (sun elevation triggers)
- Shelly RGBW animations (continuous loop, cycle, wave, chase, pulse, rock)
- Light Salon MQTT control

**BoneIO Hardware & Entity Mappings:**
- **BoneIO 32x10 Relay Board (`boneio-32-l-07` - 230V Relays):**
  - **Oświetlenie (Lights):**
    - `light.boneio_32_l_07_new_light_01`: Wejście (Wiatrołap Główne)
    - `light.boneio_32_l_07_new_light_05`: **Kuchnia Wyspa** (lampa wisząca nad wyspą)
    - `light.boneio_32_l_07_new_light_06`: Jadalnia (lampa nad stołem)
    - `light.boneio_32_l_07_new_light_12`: Podjazd (oświetlenie podjazdu)
  - **Zasilacze Oświetlenia (Power Supplies - EXCLUDED from All Lights Off scripts):**
    - `light.boneio_32_l_07_new_light_18` / `switch.zasilanie_salon_dodatkowe`: **Zasilanie Salon Dodatkowe**
    - `light.boneio_32_l_07_new_light_07` / `switch.zasilanie_sypialnia_garderoba`: **Zasilanie Sypialnia Garderoba**
    - `light.boneio_32_l_07_new_light_20` / `switch.zasilanie_mala_lazienka`: **Zasilanie Mała Łazienka**
    - `light.boneio_32_l_07_new_light_11` / `switch.zasilanie_taras`: **Zasilanie Taras**
- **BoneIO 8ch LED Dimmer (`boneio-dr-8ch-03-2c7fbc` - 24V PWM Dimmer):**
  - `light.boneio_dr_8ch_03_2c7fbc_chl_01`: Korytarz Wejściowy
  - `light.boneio_dr_8ch_03_2c7fbc_chl_02`: Korytarz Sypialnie
  - `light.boneio_dr_8ch_03_2c7fbc_chl_04`: Kuchnia Główne
  - `light.boneio_dr_8ch_03_2c7fbc_chr_01`: Korytarz Ściana
  - `light.boneio_dr_8ch_03_2c7fbc_chr_02`: Mała Łazienka Dekoracyjne
  - `light.boneio_dr_8ch_03_2c7fbc_chr_04`: **Korytarz Lustro** (Lustro w korytarzu wejściowym)
- **Taras Dual Lamps (`light.taras_lampy`):**
  - Combines `light.bulb_1` (*Taras Lampa Lewa*) and `light.bulb_2` (*Taras Lampa Prawa*) into a unified light group toggled together from dashboard and voice.

### Security/Alarm Automations
- Alarm trigger on door/windows breach
- Tag-based door access (RFID tags)
- Door/window reed sensor controls

### Roller Blind Automations
- Bedroom roller up/down control
- Sunrise/sunset scheduling

### Energy/Pricing
- Daily Pstryk price notification (20:00)
- Tesla Charging Best Window automation
- Pstryk Best Window Voice Announcement (`pstryk_best_window_voice_announcement` - plays dynamic TTS announcement on Home Assistant Voice PE `media_player.home_assistant_voice_0a9bfd_media_player` via Nabu Casa Cloud TTS)

### Voice Assistant & Radio Playback
- **Hardware:** Home Assistant Voice PE (`media_player.home_assistant_voice_0a9bfd_media_player`)
- **Voice Intents:**
  - `WlaczRadio`, `ZatrzymajRadio` (custom sentences in `config/custom_sentences/pl/dom_sentences.yaml` and `config/custom_sentences/en/dom_sentences.yaml`)
  - `GlosniejRadio`, `CiszejRadio` (*"głośniej radio"*, *"podgłośnij radio"*, *"ciszej radio"*, *"ściasz radio"* / *"volume up radio"*, *"radio volume down"*)
- **Stateful Memory:** `input_select.last_radio_station` stores the last active station (resumed when saying *"włącz radio"* / *"play radio"* without specifying a station; defaults to Eska Rock on initial run)
- **Scripts:**
  - `script.play_radio`: resolves stream URL, updates helper, streams directly on Voice PE via `media_player.play_media` (ESPHome Voice PE does not support `media_player.turn_on`)
  - `script.stop_radio`: stops Voice PE stream cleanly via `media_player.media_stop` (ESPHome does not support `media_player.turn_off`)
  - `script.toggle_radio`: smart toggle checking if Voice PE is playing -> `script.stop_radio`, otherwise -> `script.play_radio`
  - `script.radio_volume_up`: increases volume on Voice PE via `media_player.volume_up`
  - `script.radio_volume_down`: decreases volume on Voice PE via `media_player.volume_down`
- **Supported Stations:** Eska Rock (default), RMF FM, Radio ZET, Antyradio, Radio 357, TOK FM, VOX FM, Polskie Radio Trójka
- **Automations:**
  - `morning_radio_schedule`: Mon-Fri at `input_datetime.pora_pobudka` -> Radio ZET; Sat-Sun at `input_datetime.pora_pobudka_weekend` -> Antyradio on Voice PE
  - `radio_station_changed_auto_play`: Automatically switches radio stream when user selects a different station in `input_select.last_radio_station` while radio is playing
  - `voice_pe_media_playback_intercept`: Intercepts HA `call_service` events (`media_play`, `media_play_pause`, `media_pause`) targeting Voice PE and maps them to `script.play_radio`, `script.toggle_radio`, and `script.stop_radio`

### Multimedia & TV Control (Salon TCL Google TV)
- **Primary Entities:** `media_player.salon_2`, `remote.salon_2` (Android TV Remote integration)
- **Cast Target:** `media_player.googletv8897_2` (Google Cast integration for camera streaming)
- **Streaming Apps:** Netflix (`com.netflix.ninja`), YouTube (`https://www.youtube.com`), Disney+ (`com.disney.disneyplus`), Max (`https://play.max.com`), Spotify (`spotify://`), Prime Video (`https://app.primevideo.com`)
- **Camera Streaming (5 cameras):**
  - Podwórze: `camera.reolink_duo_floodlight_poe_plynny_2`
  - Wejście: `camera.rlc_822a_plynny`
  - Taras: `camera.taras_plynny`
  - Ogród: `camera.rlc_820a_niska_rozdzielczosc`
  - Front / Brama: `camera.520a_plynny`
- **Scripts:**
  - `script.tv_launch_app`: Launches streaming apps by key on `media_player.salon_2` via `remote.turn_on` and `media_player.play_media`
  - `script.tv_show_camera`: Streams camera via `camera.play_stream` (format: HLS) to `media_player.googletv8897_2`, or stops stream (`camera: stop`)
  - `script.salon_cinema_mode`: Turns on TV and activates `scene.salon_kino_1`. If after dark (`after: input_datetime.pora_zmroku, before: input_datetime.pora_switu`), sets `input_select.salon_kolor_wybor` to option 2 (`script.light_salon_color_2`), executes it, and turns off ceiling light `light.salon_mqtt`
- **Dashboard View:** `/dashboard-home/media` in `config/dashboard_modern_reference.yaml` (Dom iPad Modern) and `config/dashboard_home_improved.yaml` featuring side-by-side TV controls (tile with volume slider, Cinema Mode toggle, power toggle) and Radio Voice PE component (tile with volume slider and toggle tap action, 2-column Włącz / Wyłącz action grid, reactive station dropdown selector), 6-app quick launch grid, scenes & mood grid, conditional D-pad remote, and 5 camera glance cards with Cast / Stop buttons
- **Voice Intents:**
  - `WlaczAplikacjeTV`: *"włącz [aplikacja] na telewizorze"* / *"open [app] on tv"*
  - `GlosniejTV`, `CiszejTV`, `WyciszTV`: *"głośniej / ciszej / wycisz telewizor"* / *"volume up / down / mute tv"*
  - `PokazKamereTV`, `ZatrzymajKamereTV`: *"pokaż [kamera] na telewizorze"* / *"show [camera] on tv"*, *"zamknij podgląd kamery"*
  - `TrybKinoSalon`, `ZatrzymajKinoSalon`: *"tryb kino w salonie"* / *"cinema mode in living room"*, *"wyłącz tryb kino"*

### Image Recognition & Face Recognition
- Camera snapshot with Google AI analysis
- **Proposal & Roadmap:** [`CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md`](CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md) — Generic Camera AI analysis engine (`script.camera_ai_analyze`) with biometric face recognition, known faces comparison against reference portraits, and automated door/gate action

## Docker Commands

```bash
# Start/stop Home Assistant
docker-compose up -d
docker-compose down

# View logs
docker logs -f homeassistant-homeassistant-1

# Shell access
docker exec -it homeassistant-homeassistant-1 sh

# Start Zigbee2MQTT
./docker-zigbee2mqtt.sh

# View Zigbee2MQTT logs
docker logs -f homeassistant-zigbee2mqtt-1
```

**WARNING:** `clear_retained_simple.sh` contains hardcoded MQTT credentials (MQTT_PASS="waders").

## Key Secrets (secrets.yaml)

```yaml
latitude_home: 52.34453253634735
longitude_home: 21.099288761615757
Camera_URL: "http://10.20.2.7/cgi-bin/api.cgi?..."
CameraFront_Source: "rtsp://homeAssistant:waders@10.20.2.7:554/..."
# Two separate Pstryk installations:
pstryk_api_key_dol: "sk-BT59VQSTTHMNIRBL26B52YLCW3NMOG3YEUVDL4K5"    # dół
pstryk_api_key_gora: "sk-G0SUY5HUO5YYQXOUYS2Z7BWT0KG8SGV3Q0CGRKHW"  # góra
```

## Energy Pricing (Pstryk)

**Script:** `config/pstryk_pricing.py`
**API:** Polish energy provider integration
**Update Interval:** 3600 seconds (hourly)
**Two installations:** dół (lower/ground floor) and góra (upper floor)

**Sensors:**
- `sensor.pstryk_price_meter_dol` - Current energy price dół (PLN/kWh)
- `sensor.pstryk_price_meter_gora` - Current energy price góra (PLN/kWh)
- `sensor.pstryk_best_window_dol` - Cheapest window for dół
- `sensor.pstryk_best_window_gora` - Cheapest window for góra
- `binary_sensor.pstryk_in_best_window_dol` - ON when dół is in optimal window
- `binary_sensor.pstryk_in_best_window_gora` - ON when góra is in optimal window

**Automations:**
- `daily_energy_price_notification` - Sends Polish summary at 22:00 (uses dół sensor, pricing is identical)
- `Tesla Charging Best Window` - triggers on `binary_sensor.pstryk_in_best_window_dol`
- `pstryk_best_window_voice_announcement` - triggers on `binary_sensor.pstryk_in_best_window_dol` to speak dynamic announcement on Home Assistant Voice PE speaker

## Important Files

| File | Purpose |
|------|---------|
| `AI_DEVELOPMENT_GUIDELINES.md` | Mandatory AI development rules, planning gates, doc sync protocols |
| `llms.txt` | Standard LLM & human project index and architecture map |
| `PLACES_AND_BEHAVIORS.md` | Room-by-room entity directory & behavioral map |
| `AGENT.md` | Unified Home Assistant & ESP32/Node.js ecosystem bridge |
| `docker-compose.yml` | Home Assistant container definition |
| `docker-zigbee2mqtt.sh` | Zigbee2MQTT start script |
| `config/mqtt.yaml` | All MQTT entity configurations |
| `config/automations.yaml` | 60+ automation rules |
| `config/scripts.yaml` | Operational scripts (radio, TV, blinds, lighting) |
| `config/secrets.yaml` | Sensitive configuration |
| `data/configuration.yaml` | Zigbee2MQTT settings |

## Security Notes

- **CRITICAL:** `clear_retained_simple.sh` has hardcoded MQTT credentials
- MQTT runs on plain port 1883 without TLS
- Home Assistant uses `network_mode: host` - direct network access
- Zigbee network key stored in plain configuration

## Related Documentation

- `AI_DEVELOPMENT_GUIDELINES.md` - Authoritative AI development guidelines & protocols
- `llms.txt` - Standard LLM & human ecosystem overview
- `PLACES_AND_BEHAVIORS.md` - Complete place/room behavioral map & entity directory
- `AGENT.md` - Unified bridge between Home Assistant & ESP32/Node.js edge
- `CAMERA_AI_FACE_RECOGNITION_PROPOSAL.md` - Generic Camera AI & Face Recognition Engine proposal
- `VOICE_AI_IMPROVEMENTS_PROPOSAL.md` - Voice AI & entity remediation proposal
- `/Users/wtrzonkowski/Desktop/private/ARCHITECTURE.md` - Technical architecture
- `/Users/wtrzonkowski/Desktop/private/HOME_AUTOMATION_ECOSYSTEM.md` - System overview
- `/Users/wtrzonkowski/Desktop/private/ISSUES.md` - Known issues
- `/Users/wtrzonkowski/Desktop/private/home_automation/` - ESP32/Node.js backend
