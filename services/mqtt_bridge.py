#!/usr/bin/env python3
"""
MQTT bridge service for EB7000.

- Periodically reads all values via eb7000.core.read_all_web_ui_values()
- Publishes JSON to MQTT
- Listens for mode-set commands and calls eb7000.core.set_hk_mode()/set_fwe_mode()

This file is intended to live in a separate repository together with the
`eb7000` package. In this monorepo it imports eb7000.core via a thin shim.
"""

import json
import os
import time
from typing import Any, Dict, List

import paho.mqtt.client as mqtt

from eb7000.core import (
    read_all_web_ui_values,
    detect_present_objects,
    set_hk_mode,
    set_fwe_mode,
    set_hk_mode_std,
    HK_MODE_NAMES,
    FWE_MODE_NAMES,
    build_hk_urlaub_fc4c_hex,
)


MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "eb7000").rstrip("/")

MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant").rstrip("/")
MQTT_DISCOVERY_ENABLE = os.getenv("MQTT_DISCOVERY_ENABLE", "true").lower() in ("1", "true", "yes", "on")

AVAILABILITY_TOPIC = f"{MQTT_BASE_TOPIC}/status"
PAYLOAD_AVAILABLE = "ready"
PAYLOAD_NOT_AVAILABLE = "lost"

EB7000_HOST = os.getenv("EB7000_HOST", "192.168.0.9")
EB7000_PORT = int(os.getenv("EB7000_PORT", "502"))

# Base name used by Home Assistant when building sensor entity_ids, via device name.
# Example: sensor.{EB7000_ENTITY_BASENAME}_fwe_eintritt_waermetauscher
EB7000_ENTITY_BASENAME = os.getenv("EB7000_ENTITY_BASENAME", "EB7000 Heating Controller")
ID_PREFIX = os.getenv("ID_PREFIX", "eb7000")
SENSOR_PREFIX = os.getenv("SENSOR_PREFIX", "EB7000")

if len(ID_PREFIX) > 0 and not ID_PREFIX.endswith("_"):
    ID_PREFIX += "_"

if len(SENSOR_PREFIX) > 0 and not SENSOR_PREFIX.endswith(" "):
    SENSOR_PREFIX += " "

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

# Comma-separated list of object keys to register in Home Assistant.
# Leave unset (or empty) to auto-detect from the device on startup.
# Override example: ENABLED_OBJECTS=hk1,hk2,fwe,sp
_raw_enabled = os.getenv("ENABLED_OBJECTS", "").strip()
ENABLED_OBJECTS: set = {item.strip().lower() for item in _raw_enabled.split(",") if item.strip()}

# Sensor metadata helpers
_wt: Dict[str, Any] = {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement", "icon": "mdi:thermometer-water"}
_ot: Dict[str, Any] = {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement", "icon": "mdi:thermometer"}
_fl: Dict[str, Any] = {"unit_of_measurement": "L/min", "state_class": "measurement", "icon": "mdi:water-pump"}
_pw: Dict[str, Any] = {"unit_of_measurement": "kW", "device_class": "power", "state_class": "measurement", "icon": "mdi:solar-power"}

# Catalog of all known sensors per object type.
# Keys are the field names as they appear in the JSON state (from extract_* functions).
SENSOR_CATALOG: Dict[str, List[tuple]] = {
    "hk": [
        ("Vorlauftemperatur", _wt),
        ("Rücklauftemperatur", _wt),
        ("Vorlaufanforderung", _wt),
    ],
    "fwe": [
        ("Kaltwasser_&_Zirkulation", _wt),
        ("Warmwasser", _wt),
        ("Eintritt_Wärmetauscher", _wt),
        ("Zapfmenge", _fl),
    ],
    "sp": [
        ("FWE_Niveau", _wt),
        ("HT_Niveau", _wt),
        ("NT_Niveau", _wt),
        ("SP_unten", _wt),
        ("Außentemperatur", _ot),
    ],
    "wq": [
        ("Betriebstemperatur", _ot),
        ("Rücklauftemperatur", _ot),
    ],
    "sk": [
        ("Kollektortemperatur_F1", _ot),
        ("Warmtemperatur", _ot),
        ("Kalttemperatur", _ot),
        ("Nutztemperatur", _ot),
        ("Solardurchfluss", _fl),
        ("Leistung_kW", _pw),
    ],
    "wpint": [
        ("Vorlauftemperatur", _wt),
        ("SoleKalt", _ot),
        ("KühlspeicherOben", _ot),
        ("KühlspeicherUnten", _ot),
    ],
    "wpsiem": [
        ("Vorlauftemperatur", _wt),
        ("Rücklauftemperatur", _wt),
        ("Quellenaustritt", _ot),
        ("Quelleneintritt", _ot),
        ("KühlspeicherUnten", _ot),
    ],
    "wp4000": [
        ("Abtau", _ot),
        ("QuellenEIN", _ot),
        ("QuellenAUS", _ot),
        ("WP_Vorlauf", _wt),
        ("WP_Rücklauf", _wt),
    ],
}

_discovery_done = False
# Last requested Urlaub days per HK, used when sending mode=Urlaub.
_urlaub_days: Dict[int, int] = {}


def _make_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="eb7000-mqtt-bridge")
    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.will_set(AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE, qos=1, retain=True)
    return client


def _on_connect(client: mqtt.Client, userdata: Any, connect_flags: Any, reason_code: Any, properties: Any) -> None:
    base = MQTT_BASE_TOPIC
    client.subscribe(f"{base}/cmd/hk/+/mode", qos=1)
    client.subscribe(f"{base}/cmd/hk/+/urlaub_days", qos=1)
    client.subscribe(f"{base}/cmd/fwe/mode", qos=1)
    client.publish(AVAILABILITY_TOPIC, PAYLOAD_AVAILABLE, qos=1, retain=True)


def _on_disconnect(client: mqtt.Client, userdata: Any, disconnect_flags: Any, reason_code: Any, properties: Any = None) -> None:
    print(f"[WARN] MQTT disconnected: {reason_code}")


def _on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    topic = msg.topic
    payload = msg.payload.decode(errors="ignore").strip()
    base = MQTT_BASE_TOPIC

    # Handle Urlaub days commands first: eb7000/cmd/hk/<n>/urlaub_days
    if topic.startswith(f"{base}/cmd/hk/") and topic.endswith("/urlaub_days"):
        try:
            middle = topic[len(f"{base}/cmd/hk/") : -len("/urlaub_days")]
            hk_num = int(middle)
        except Exception:
            return
        try:
            days = int(payload)
        except ValueError:
            return
        if days <= 0 or days > 365:
            return
        _urlaub_days[hk_num] = days
        client.publish(f"{base}/hk{hk_num}/urlaub_days", days, qos=1, retain=True)
        return

    if topic.startswith(f"{base}/cmd/hk/") and topic.endswith("/mode"):
        try:
            middle = topic[len(f"{base}/cmd/hk/") : -len("/mode")]
            hk_num = int(middle)
        except Exception:
            return
        mode = _parse_mode_payload(payload)
        if mode is None:
            return
        # If Urlaub is requested (mode 3), include the currently configured days.
        # The original web UI defaults to 13 days.
        urlaub_days = None
        if mode == 3:
            urlaub_days = _urlaub_days.get(hk_num, 13)
            _urlaub_days[hk_num] = urlaub_days
            client.publish(f"{base}/hk{hk_num}/urlaub_days", urlaub_days, qos=1, retain=True)

        # Debug: publish the exact web-ui-like FC4C frame for Urlaub
        if mode == 3 and urlaub_days is not None:
            hex_cmd = build_hk_urlaub_fc4c_hex(hk_num, urlaub_days)
            if hex_cmd is not None:
                client.publish(f"{base}/debug/last_hk{hk_num}_urlaub_fc4c_hex", hex_cmd, qos=1, retain=True)

        ok = set_hk_mode(hk_num, mode, urlaub_days=urlaub_days, host=EB7000_HOST, port=EB7000_PORT)
        # Fallback: some firmwares accept the standard Modbus write for Urlaub.
        if not ok and mode == 3 and urlaub_days is not None:
            ok = set_hk_mode_std(hk_num, mode, urlaub_days=urlaub_days, host=EB7000_HOST, port=EB7000_PORT)
        if ok:
            # Update HK mode_name topic directly so the select reflects
            # the new value without forcing a full state read.
            mode_name = HK_MODE_NAMES.get(mode, f"unknown({mode})")
            client.publish(f"{base}/hk{hk_num}/mode_name", mode_name, qos=1, retain=True)
        return

    if topic == f"{base}/cmd/fwe/mode":
        mode = _parse_mode_payload(payload, is_fwe=True)
        if mode is None:
            return
        ok = set_fwe_mode(mode, host=EB7000_HOST, port=EB7000_PORT)
        if ok:
            mode_name = FWE_MODE_NAMES.get(mode, f"unknown({mode})")
            client.publish(f"{base}/fwe/mode_name", mode_name, qos=1, retain=True)


def _parse_mode_payload(payload: str, is_fwe: bool = False) -> int | None:
    payload_l = payload.lower()
    if payload.isdigit():
        val = int(payload)
        if is_fwe and val in (0, 1):
            return val
        if not is_fwe and val in (0, 1, 2, 3, 4):
            return val
        return None

    mapping_hk = {
        "automatik": 0,
        "auto": 0,
        "party": 1,
        "frost": 2,
        "frostschutz": 2,
        "urlaub": 3,
        "anheben": 4,
    }
    mapping_fwe = {
        "automatik": 0,
        "auto": 0,
        "spar": 1,
        "eco": 1,
    }
    if is_fwe:
        return mapping_fwe.get(payload_l)
    return mapping_hk.get(payload_l)


def publish_state(client: mqtt.Client) -> None:
    global _discovery_done

    base = MQTT_BASE_TOPIC
    data = read_all_web_ui_values(EB7000_HOST, EB7000_PORT, use_standard_modbus=False)

    # Publish discovery once we have a valid example payload
    if MQTT_DISCOVERY_ENABLE and not _discovery_done and isinstance(data, dict):
        publish_discovery(client)
        _discovery_done = True

    payload = json.dumps(data)
    client.publish(f"{base}/state", payload, qos=1, retain=True)

    # Also publish individual mode_name topics used by HA selects,
    # so they stay in sync even if the full state lags.
    try:
        for obj_key in ENABLED_OBJECTS:
            if obj_key.startswith("hk") and obj_key[2:].isdigit():
                mode = data.get(obj_key, {}).get("mode_name")
                if mode is not None:
                    client.publish(f"{base}/{obj_key}/mode_name", mode, qos=1, retain=True)
        if "fwe" in ENABLED_OBJECTS:
            fwe_mode = data.get("fwe", {}).get("mode_name")
            if fwe_mode is not None:
                client.publish(f"{base}/fwe/mode_name", fwe_mode, qos=1, retain=True)
    except Exception as exc:
        print(f"[WARN] mode topic publish failed: {exc}")


def _device_info() -> Dict[str, Any]:
    return {
        "identifiers": ["eb7000"],
        "name": EB7000_ENTITY_BASENAME,
        "manufacturer": "EBM",
        "model": "EB7000",
    }


def _slugify(part: str) -> str:
    s = part.strip().lower()
    out_chars: List[str] = []
    umlaut_map = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "&": "_",
    }
    for ch in s:
        mapped = umlaut_map.get(ch, ch)
        for mch in mapped:
            out_chars.append(mch if mch.isalnum() else "_")
    slug = "".join(out_chars).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "value"


def publish_discovery(client: mqtt.Client) -> None:
    """
    Publish Home Assistant MQTT discovery configs for all objects listed in ENABLED_OBJECTS.

    Each object type has a fixed sensor catalog (SENSOR_CATALOG). HK circuits also get
    a mode select and urlaub-days number entity. FWE gets a mode select.

    Discovery path follows HA convention: {prefix}/{component}/{node_id}/{object_id}/config
    """
    prefix = MQTT_DISCOVERY_PREFIX
    base = MQTT_BASE_TOPIC
    node_id = MQTT_BASE_TOPIC  # groups all entities under one device node in the topic hierarchy
    device = _device_info()

    def _pub(path: str, payload: Dict[str, Any]) -> None:
        payload = {
            **payload,
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": PAYLOAD_AVAILABLE,
            "payload_not_available": PAYLOAD_NOT_AVAILABLE,
        }
        client.publish(f"{prefix}/{path}", json.dumps(payload), qos=1, retain=True)

    # Determine enabled HK circuit numbers
    hk_ids = sorted(
        int(k[2:]) for k in ENABLED_OBJECTS if k.startswith("hk") and k[2:].isdigit()
    )

    # Sensors: one entity per catalog entry per enabled object
    for obj_key in sorted(ENABLED_OBJECTS):
        obj_type = "hk" if (obj_key.startswith("hk") and obj_key[2:].isdigit()) else obj_key
        for field_name, meta in SENSOR_CATALOG.get(obj_type, []):
            field_slug = _slugify(field_name)
            unique_id = f"{ID_PREFIX}{obj_key}_{field_slug}"
            field_display = field_name.replace("_", " ")
            name = f"{SENSOR_PREFIX}{obj_key.upper()} {field_display}"
            value_template = f'{{{{ value_json["{obj_key}"]["{field_name}"] }}}}'
            _pub(f"sensor/{node_id}/{obj_key}_{field_slug}/config", {
                "name": name,
                "state_topic": f"{base}/state",
                "value_template": value_template,
                "unique_id": unique_id,
                "device": device,
                **meta,
            })

    # Mode selects and urlaub-days numbers for enabled HK circuits
    for hk_id in hk_ids:
        hk_key = f"hk{hk_id}"
        select_id = f"{ID_PREFIX}{hk_key}_mode"
        _pub(f"select/{node_id}/{hk_key}_mode/config", {
            "name": f"{SENSOR_PREFIX}HK{hk_id} Modus",
            "state_topic": f"{base}/{hk_key}/mode_name",
            "command_topic": f"{base}/cmd/hk/{hk_id}/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Party", "Frostschutz", "Urlaub", "Anheben"],
            "unique_id": select_id,
            "device": device,
        })
        number_id = f"{ID_PREFIX}{hk_key}_urlaub_days"
        _pub(f"number/{node_id}/{hk_key}_urlaub_days/config", {
            "name": f"{SENSOR_PREFIX}HK{hk_id} Urlaubstage",
            "state_topic": f"{base}/{hk_key}/urlaub_days",
            "command_topic": f"{base}/cmd/hk/{hk_id}/urlaub_days",
            "min": 1, "max": 365, "step": 1, "mode": "box",
            "unit_of_measurement": "d",
            "unique_id": number_id,
            "device": device,
        })

    # FWE mode select
    if "fwe" in ENABLED_OBJECTS:
        select_id = f"{ID_PREFIX}fwe_mode"
        _pub(f"select/{node_id}/fwe_mode/config", {
            "name": f"{SENSOR_PREFIX}FWE Modus",
            "state_topic": f"{base}/fwe/mode_name",
            "command_topic": f"{base}/cmd/fwe/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Spar"],
            "unique_id": select_id,
            "device": device,
        })


def main() -> None:
    global ENABLED_OBJECTS

    if not ENABLED_OBJECTS:
        print("ENABLED_OBJECTS not set — auto-detecting present objects...")
        for attempt in range(1, 4):
            detected = detect_present_objects(EB7000_HOST, EB7000_PORT)
            if detected:
                ENABLED_OBJECTS = detected
                print(f"Detected: {sorted(ENABLED_OBJECTS)}")
                break
            print(f"Detection attempt {attempt}/3 failed, retrying in 10 s...")
            time.sleep(10)
        else:
            print("Auto-detection failed — check EB7000 connection. No HA entities will be created.")
    else:
        print(f"Using configured objects: {sorted(ENABLED_OBJECTS)}")

    client = _make_client()
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    try:
        while True:
            try:
                publish_state(client)
            except Exception as exc:
                print(f"[ERROR] publish_state failed: {exc}")
            now = time.time()
            next_target = (now // POLL_INTERVAL + 1) * POLL_INTERVAL
            time.sleep(next_target - now)
    finally:
        # A clean disconnect() below suppresses the broker-side LWT, so publish
        # the "lost" availability ourselves for graceful shutdowns. Ungraceful
        # deaths still fall back to the LWT set in _make_client().
        try:
            client.publish(
                AVAILABILITY_TOPIC, PAYLOAD_NOT_AVAILABLE, qos=1, retain=True
            ).wait_for_publish(timeout=2)
        except Exception:
            pass
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()

