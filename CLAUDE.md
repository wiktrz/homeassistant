# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Home Assistant deployment project using Docker for home automation control. It integrates with the `home_automation` project which contains ESP32 heating controllers and Node.js backend.

**Key Components:**
- **Home Assistant** - Main UI, automations, MQTT sensors/climate controls
- **Zigbee2MQTT** - Zigbee device integration (Sonoff Zigbee 3.0 USB Dongle Plus V2)
- **Pstryk Energy Pricing** - Dynamic energy pricing from Polish provider

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

### Lighting Automations
- Motion-triggered lights (PIR sensors)
- Time-based dimming (sun elevation triggers)
- Shelly RGBW animations (continuous loop, cycle, wave, chase, pulse, rock)
- Light Salon MQTT control

### Security/Alarm Automations
- Alarm trigger on door/windows breach
- Tag-based door access (RFID tags)
- Door/window reed sensor controls

### Roller Blind Automations
- Bedroom roller up/down control
- Sunrise/sunset scheduling

### Energy/Pricing
- Daily Pstryk price notification (20:00)

### Image Recognition
- Camera snapshot with Google AI analysis

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

## Important Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Home Assistant container definition |
| `docker-zigbee2mqtt.sh` | Zigbee2MQTT start script |
| `config/mqtt.yaml` | All MQTT entity configurations |
| `config/automations.yaml` | 60+ automation rules |
| `config/secrets.yaml` | Sensitive configuration |
| `data/configuration.yaml` | Zigbee2MQTT settings |

## Security Notes

- **CRITICAL:** `clear_retained_simple.sh` has hardcoded MQTT credentials
- MQTT runs on plain port 1883 without TLS
- Home Assistant uses `network_mode: host` - direct network access
- Zigbee network key stored in plain configuration

## Related Documentation

- `/Users/wtrzonkowski/Desktop/private/ARCHITECTURE.md` - Technical architecture
- `/Users/wtrzonkowski/Desktop/private/HOME_AUTOMATION_ECOSYSTEM.md` - System overview
- `/Users/wtrzonkowski/Desktop/private/ISSUES.md` - Known issues
- `/Users/wtrzonkowski/Desktop/private/home_automation/` - ESP32/Node.js backend
