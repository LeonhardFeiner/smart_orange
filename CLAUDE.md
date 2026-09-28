# smart_orange

MQTT bridge service that connects an **EBM EB7000 heating controller** to MQTT / Home Assistant via Modbus TCP.

## Architecture

```
EB7000 heating controller
      │  Modbus TCP
      ▼
eb7000/core.py        ← low-level Modbus client + device logic
      │
      ▼
services/mqtt_bridge.py  ← polls every 30 s, publishes to MQTT, handles commands
      │
      ▼
MQTT broker  →  Home Assistant (auto-discovery)
```

## Project Structure

```
eb7000/
  __init__.py         # public API exports
  core.py             # Modbus TCP implementation, device discovery, data extraction
services/
  mqtt_bridge.py      # MQTT service entry point
docker/
  Dockerfile.mqtt     # production image (python:3.11-slim + paho-mqtt)
docker-compose.yml    # local deployment
.env.example          # all required env vars with defaults
homeassistant_view.yaml  # example HA dashboard
```

## Running the Service

```bash
cp .env.example .env
# edit .env — at minimum set EB7000_HOST
docker compose up -d
```

## Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `EB7000_HOST` | — | IP of the heating controller (required) |
| `EB7000_PORT` | `502` | Modbus TCP port |
| `MQTT_HOST` | `mosquitto` | MQTT broker |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | Auth credentials |
| `MQTT_BASE_TOPIC` | `eb7000` | Root topic prefix |
| `MQTT_DISCOVERY_ENABLE` | `true` | Publish HA discovery configs |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | HA discovery prefix |
| `POLL_INTERVAL` | `30` | Seconds between Modbus reads |
| `EB7000_ENTITY_BASENAME` | — | Device name shown in HA |
| `SENSOR_PREFIX` | — | Prefix for entity names |
| `ENABLED_OBJECTS` | auto | Whitelist of component IDs to monitor |

## Core Module: `eb7000/core.py`

### Device Discovery

On startup, the bridge auto-detects installed components by querying Modbus parameters:

- `HK1`–`HK4` — heating circuits (Heizkreis)
- `FWE` — fresh water / hot water (Frischwasser)
- `SK` — solar circuit
- `WQ` — heat source (Wärmequelle)
- `SP` — storage tank (Speicher)
- `WPint`, `WPsiem`, `EB4000` — heat pumps

### Modbus Functions Used

| Code | Usage |
|---|---|
| FC03 | Read holding registers |
| FC04 | Read input registers |
| FC4A | Efficient batch read (preferred; falls back to FC04) |
| FC06 | Write single register |
| FC16 | Write multiple registers |
| FC4C | Extended write |

### Data Extraction Functions

- `extract_hk_values()` — temperatures, mode, pause for a heating circuit
- `extract_fwe_values()` — cold/hot water, exchanger, tap flow
- `extract_sk_values()` — solar collector/tank temps, flow rate, power
- `extract_wpsiem_values()` — Siemens heat pump data
- `extract_wp4000_values()` — EB4000 heat pump data

Sentinel value `31500` means "sensor disconnected" and is filtered out before publishing.

## MQTT Topics

| Topic | Direction | Content |
|---|---|---|
| `{base}/state` | publish | Full JSON state of all sensors |
| `{base}/{component}/mode` | publish | Current mode name string |
| `{base}/{component}/urlaub_days` | publish | Vacation days count |
| `{base}/cmd/hk/{N}/mode` | subscribe | Set heating circuit N mode |
| `{base}/cmd/hk/{N}/urlaub_days` | subscribe | Set vacation days for HK N |
| `{base}/cmd/fwe/mode` | subscribe | Set fresh water mode |

## Supported Modes

**Heating circuits (HK):**
`Automatik` (0), `Party` (1), `Frostschutz` (2), `Urlaub` (3), `Anheben` (4)

**Fresh water (FWE):**
`Automatik` (0), `Spar` (1)

## CI/CD

GitHub Actions workflow (`.github/workflows/docker-publish.yml`) builds and pushes multi-platform images (`linux/amd64`, `linux/arm64`) to Docker Hub on pushes to `main` and on version tags.

## Dependencies

- `paho-mqtt` — only runtime dependency (see `requirements.txt`)
- Python 3.11

## Notes

- German naming is used throughout for component types and modes (matching EB7000 firmware conventions).
- `ENABLED_OBJECTS` can be set to restrict which components are polled — useful for faster polling or partial installations.
- The bridge publishes Home Assistant MQTT discovery payloads on startup so entities appear automatically without manual HA configuration.
