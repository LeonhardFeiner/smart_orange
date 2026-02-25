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
from typing import Any, Dict, Iterable, List

import paho.mqtt.client as mqtt

from eb7000.core import (
    read_all_web_ui_values,
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

_discovery_done = False
# Last requested Urlaub days per HK, used when sending mode=Urlaub.
_urlaub_days: Dict[int, int] = {}


def _make_client() -> mqtt.Client:
    client = mqtt.Client()
    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    return client


def _on_connect(client: mqtt.Client, userdata: Any, flags: Dict[str, Any], rc: int) -> None:
    base = MQTT_BASE_TOPIC
    client.subscribe(f"{base}/cmd/hk/+/mode")
    client.subscribe(f"{base}/cmd/fwe/mode")


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
        publish_discovery(client, data)
        _discovery_done = True

    payload = json.dumps(data)
    client.publish(f"{base}/state", payload, qos=1, retain=True)

    # Also publish individual mode_name topics used by HA selects,
    # so they stay in sync even if the full state lags.
    try:
        hk1_mode = data.get("hk1", {}).get("mode_name")
        hk2_mode = data.get("hk2", {}).get("mode_name")
        hk3_mode = data.get("hk3", {}).get("mode_name")
        fwe_mode = data.get("fwe", {}).get("mode_name")
        if hk1_mode is not None:
            client.publish(f"{base}/hk1/mode_name", hk1_mode, qos=1, retain=True)
        if hk2_mode is not None:
            client.publish(f"{base}/hk2/mode_name", hk2_mode, qos=1, retain=True)
        if hk3_mode is not None:
            client.publish(f"{base}/hk3/mode_name", hk3_mode, qos=1, retain=True)
        if fwe_mode is not None:
            client.publish(f"{base}/fwe/mode_name", fwe_mode, qos=1, retain=True)
    except Exception:
        # Never let per-topic publishing break the main state loop.
        pass


def _device_info() -> Dict[str, Any]:
    return {
        "identifiers": ["eb7000"],
        "name": EB7000_ENTITY_BASENAME,
        "manufacturer": "EBM",
        "model": "EB7000",
    }


def publish_discovery(client: mqtt.Client, example_state: Dict[str, Any]) -> None:
    """
    Publish Home Assistant MQTT discovery configs.

    This creates entities for (almost) all numeric values
    in the EB7000 state payload, plus a few convenient mode
    selects for HK1 and FWE.
    """

    prefix = MQTT_DISCOVERY_PREFIX
    base = MQTT_BASE_TOPIC
    device = _device_info()

    def _pub(path: str, payload: Dict[str, Any]) -> None:
        topic = f"{prefix}/{path}"
        client.publish(topic, json.dumps(payload), qos=1, retain=True)


    # Combined per-sensor metadata:
    # - "unit": unit_of_measurement
    # - "icon": Home Assistant icon
    watertemp: Dict[str, Any] = {"unit_of_measurement": "°C", "icon": "mdi:thermometer-water"}
    waterflow: Dict[str, Any] = {"unit_of_measurement": "l/min", "icon": "mdi:water-pump"}
    othertemp: Dict[str, Any] = {"unit_of_measurement": "°C", "icon": "mdi:thermometer"}
    skip: Dict[str, Any] = None

    sensor_overrides: Dict[str, Dict[str, Any]] = {
        # Vorlauf / Rücklauf / Vorlaufanforderung / Speicher / Warmwasser / Kaltwasser
        ("hk1", "vorlauftemperatur"): (watertemp, None),
        ("hk1", "ruecklauftemperatur"): (watertemp, None),
        ("hk1", "vorlaufanforderung"): (watertemp, None),
        ("hk2", "vorlauftemperatur"): (watertemp, None),
        ("hk2", "ruecklauftemperatur"): (watertemp, None),
        ("hk2", "vorlaufanforderung"): (watertemp, None),
        ("hk3", "vorlauftemperatur"): (watertemp, None),
        ("hk3", "ruecklauftemperatur"): (watertemp, None),
        ("hk3", "vorlaufanforderung"): (watertemp, None),
        ("fwe", "kaltwasser_zirkulation"): (watertemp, None),
        ("fwe", "warmwasser"): (watertemp, None),
        ("fwe", "eintritt_waermetauscher"): (watertemp, None),
        ("sp", "fwe_niveau"): (watertemp, None),
        ("sp", "ht_niveau"): (watertemp, None),
        ("sp", "nt_niveau"): (watertemp, None),
        ("sp", "sp_unten"): (watertemp, None),
        # Zapfmenge (flow)
        ("fwe", "zapfmenge_l_min"): (waterflow, "zapfmenge"),
        # Betriebstemperatur + Außentemperatur
        ("wq", "betriebstemperatur"): (othertemp, None),
        ("sp", "aussentemperatur"): (othertemp, None),
        # Sensors we want to skip entirely from auto-generation
        ("hk3", "pause"): skip,
        ("hk2", "pause"): skip,
        ("hk1", "pause"): skip,
        ("ak", "eb1000_count"): skip,
        ("ak", "eb4000_count"): skip,
        ("ak", "rbm8_count"): skip,
        ("ak", "wp_exist"): skip,
        ("ak", "wp_typ"): skip,
        ("hk2", "mode"): skip,
        ("hk1", "mode"): skip,
        ("hk1", "name_control_hi"): skip,
        ("hk1", "name_control_lo"): skip,
        ("hk1", "status_bits_word0"): skip,
        ("hk1", "status_bits_word8"): skip,
        ("hk2", "name_control_hi"): skip,
        ("hk2", "name_control_lo"): skip,
        ("hk3", "mode"): skip,
        ("hk3", "status_bits_word0"): skip,
        ("hk3", "status_bits_word8"): skip,
        ("hk2", "status_bits_word0"): skip,
        ("hk2", "status_bits_word8"): skip,
        ("sk", "kalttemperatur"): skip,
        ("sk", "kollektortemperatur_f1"): skip,
        ("sk", "leistung_kw"): skip,
        ("sk", "name_control_hi"): skip,
        ("sk", "name_control_lo"): skip,
        ("sk", "nutztemperatur"): skip,
        ("sk", "solardurchfluss"): skip,
        ("sk", "warmtemperatur"): skip,
        ("sp", "name_control_hi"): skip,
        ("sp", "name_control_lo"): skip,
        ("wpint", "name_control_hi"): skip,
        ("wpint", "name_control_lo"): skip,
        ("wq", "name_control_hi"): skip,
        ("wq", "name_control_lo"): skip,
        ("fwe", "mode"): skip,
        ("fwe", "name_control_hi"): skip,
        ("fwe", "name_control_lo"): skip,
    }

    def _slugify(part: str) -> str:
        s = part.strip().lower()
        out_chars: List[str] = []
        umlaut_map = {
            "ä": "ae",
            "ö": "oe",
            "ü": "ue",
            "ß": "ss",
            "Ä": "Ae",
            "Ö": "Oe",
            "Ü": "Ue",
        }
        for ch in s:
            mapped = umlaut_map.get(ch, ch)
            for mch in mapped:
                if mch.isalnum():
                    out_chars.append(mch)
                else:
                    out_chars.append("_")
        slug = "".join(out_chars).strip("_")
        while "__" in slug:
            slug = slug.replace("__", "_")
        return slug or "value"

    def _iter_numeric_paths(prefix: List[str], value: Any) -> Iterable[List[str]]:
        if isinstance(value, dict):
            for k, v in value.items():
                new_prefix = prefix + [str(k)]
                yield from _iter_numeric_paths(new_prefix, v)
        elif isinstance(value, (int, float, bool)):
            yield prefix

    # Auto-generate sensors for all numeric / boolean leaves
    if isinstance(example_state, dict):
        for path in _iter_numeric_paths([], example_state):
            if not path:
                continue
            # Build unique_id and look up per-sensor metadata
            slug_parts = tuple(_slugify(p) for p in path)
            meta_new_name = sensor_overrides.get(slug_parts, None)
            if meta_new_name is None:
                continue
            meta, new_name = meta_new_name

            unique_id = ID_PREFIX + "_".join(slug_parts)

            first_part, *other_parts = path
            if new_name is not None:
                other_parts = [new_name]

            secondary_name = " ".join(p.replace("_", " ").title() for p in other_parts)
            name = SENSOR_PREFIX + first_part.upper() + " " + secondary_name

            # Build Jinja2 value_template using dict-style access to be robust to umlauts
            path_expr = "".join(f'["{p}"]' for p in path)
            value_template = f"{{{{ value_json{path_expr} }}}}"

            payload = {
                "name": name,
                "state_topic": f"{base}/state",
                "value_template": value_template,
                "unique_id": unique_id,
                "device": device,
                **meta,
            }

            _pub(f"sensor/{unique_id}/config", payload)


    # Mode selects for HK1–HK3 and FWE
    selects: Dict[str, Dict[str, Any]] = {
        f"select/{ID_PREFIX}fwe_mode/config": {
            "name": f"{SENSOR_PREFIX}FWE Modus",
            "state_topic": f"{base}/fwe/mode_name",
            "command_topic": f"{base}/cmd/fwe/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Spar"],
            "unique_id": f"{ID_PREFIX}fwe_mode",
            "device": device,
        },
        **{
            f"select/{ID_PREFIX}hk{hk_id}_mode/config": {
                "name": f"{SENSOR_PREFIX}HK{hk_id} Modus",
                "state_topic": f"{base}/hk{hk_id}/mode_name",
                "command_topic": f"{base}/cmd/hk/{hk_id}/mode",
                "value_template": "{{ value }}",
                "options": ["Automatik", "Party", "Frostschutz", "Urlaub", "Anheben"],
                "unique_id": f"{ID_PREFIX}hk{hk_id}_mode",
                "device": device,
            } for hk_id in range(1, 4)
        }
    }

    for path, payload in selects.items():
        _pub(path, payload)

    # Urlaub days numbers for HK1–HK3
    numbers: Dict[str, Dict[str, Any]] = {
        f"number/{ID_PREFIX}_hk{hk_id}_urlaub_days/config": {
            "name": f"{SENSOR_PREFIX}HK{hk_id} Urlaubstage",
            "state_topic": f"{base}/hk{hk_id}/urlaub_days",
            "command_topic": f"{base}/cmd/hk/{hk_id}/urlaub_days",
            "min": 1,
            "max": 365,
            "step": 1,
            "mode": "box",
            "unit_of_measurement": "d",
            "unique_id": f"{ID_PREFIX}_hk{hk_id}_urlaub_days",
            "device": device,
        } for hk_id in range(1, 4)
    }

    for path, payload in numbers.items():
        _pub(path, payload)


def main() -> None:
    client = _make_client()
    client.on_connect = _on_connect
    client.on_message = _on_message

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    try:
        while True:
            try:
                publish_state(client)
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()

