# smart_orange

MQTT bridge that connects an **EBM EB7000 heating controller** to Home Assistant via Modbus TCP.

It polls the EB7000 every 30 seconds, publishes sensor values to MQTT, and auto-generates Home Assistant discovery configs so all entities appear without any manual HA configuration.

## Features

- Auto-discovers installed heating components (heating circuits, solar, heat pump, hot water, storage)
- Publishes temperatures, flow rates, power, and operating modes to MQTT
- Full [Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) support
- Remote control of heating modes and vacation days via MQTT commands
- Multi-platform Docker image (`linux/amd64`, `linux/arm64`)

## Supported Components

| ID | Component |
|---|---|
| `HK1`–`HK4` | Heating circuits (Heizkreis) |
| `FWE` | Fresh water / hot water (Frischwasser) |
| `SK` | Solar circuit |
| `WQ` | Heat source (Wärmequelle) |
| `SP` | Storage tank (Speicher) |
| `WPint`, `WPsiem`, `EB4000` | Heat pumps |

## Quick Start

```bash
cp .env.example .env
# Edit .env — set at minimum EB7000_HOST
docker compose up -d
```

## Configuration

All configuration is done via environment variables. Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `EB7000_HOST` | — | IP address of the EB7000 controller **(required)** |
| `EB7000_PORT` | `502` | Modbus TCP port |
| `MQTT_HOST` | `mosquitto` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | — | MQTT username (optional) |
| `MQTT_PASSWORD` | — | MQTT password (optional) |
| `MQTT_BASE_TOPIC` | `eb7000` | Root MQTT topic prefix |
| `MQTT_DISCOVERY_ENABLE` | `true` | Enable Home Assistant MQTT discovery |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | HA discovery prefix |
| `POLL_INTERVAL` | `30` | Seconds between Modbus reads |
| `EB7000_ENTITY_BASENAME` | — | Device name shown in Home Assistant |
| `SENSOR_PREFIX` | — | Prefix for all entity names |
| `ENABLED_OBJECTS` | auto | Comma-separated whitelist of component IDs (auto-detects if unset) |

## MQTT Topics

| Topic | Direction | Content |
|---|---|---|
| `{base}/state` | published | Full JSON snapshot of all sensor values |
| `{base}/{component}/mode` | published | Current mode name |
| `{base}/{component}/urlaub_days` | published | Vacation days remaining |
| `{base}/status` | published | Retained availability (`ready` / `lost`) — LWT on ungraceful disconnect, explicit on graceful shutdown |
| `{base}/cmd/hk/{N}/mode` | subscribed | Set heating circuit N mode |
| `{base}/cmd/hk/{N}/urlaub_days` | subscribed | Set vacation days for heating circuit N |
| `{base}/cmd/fwe/mode` | subscribed | Set fresh water mode |

### Removing stale entities

Discovery configs are retained forever once published, so an object that stops being auto-detected (or gets dropped from `ENABLED_OBJECTS`) between restarts leaves a ghost entity in HA. Clear it manually:

```sh
# List currently retained discovery topics
mosquitto_sub -h <broker> -u <user> -P <pass> -t 'homeassistant/+/eb7000/#' --retained-only -v -W 2

# Retract one (empty retained payload removes the entity from HA)
mosquitto_pub -h <broker> -u <user> -P <pass> -t 'homeassistant/sensor/eb7000/<old_object_id>/config' -n -r
```

### Operating Modes

**Heating circuits:** `Automatik`, `Party`, `Frostschutz`, `Urlaub`, `Anheben`

**Fresh water:** `Automatik`, `Spar`

## Architecture

```
EB7000 controller
      │  Modbus TCP
      ▼
eb7000/core.py          ← Modbus client, device discovery, data extraction
      │
      ▼
services/mqtt_bridge.py ← polling loop, MQTT publish/subscribe, HA discovery
      │
      ▼
MQTT broker  →  Home Assistant
```

## Requirements

- Docker & Docker Compose
- EB7000 controller accessible over the network (Modbus TCP port 502)
- MQTT broker (e.g. Mosquitto)

## License

Apache 2.0 — see [LICENSE](LICENSE).
