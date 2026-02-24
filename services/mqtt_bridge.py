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
            unit_hex = {1: "50", 2: "50", 3: "11"}.get(hk_num)
            addr_hex = {1: "2800", 2: "3000", 3: "2000"}.get(hk_num)
            if unit_hex and addr_hex:
                days_hex = f"{urlaub_days:04x}"
                hex_cmd = f"00010000000b{unit_hex}4c{addr_hex}0002040003{days_hex}"
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
        "name": "EB7000 Heating Controller",
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

    # Discovery overrides: units, names, and sensors to skip completely.
    temp_sensor_ids = {
        "eb7000_aussentemperatur",
        "eb7000_fwe_kaltwasser_zirkulation",
        "eb7000_fwe_warmwasser",
        "eb7000_fwe_eintritt_wärmetauscher",
        "eb7000_hk1_vorlaufanforderung",
        "eb7000_hk1_vorlauftemperatur",
        "eb7000_hk1_rucklauf",
        "eb7000_hk1_rücklauftemperatur",
        "eb7000_hk1_vorlauf",
        "eb7000_hk2_vorlaufanforderung",
        "eb7000_hk2_vorlauftemperatur",
        "eb7000_hk2_rücklauftemperatur",
        "eb7000_hk3_vorlaufanforderung",
        "eb7000_hk3_vorlauftemperatur",
        "eb7000_hk3_rücklauftemperatur",
        "eb7000_sp_fwe_niveau",
        "eb7000_sp_ht_niveau",
        "eb7000_sp_nt_niveau",
        "eb7000_sp_sp_unten",
        "eb7000_wq_betriebstemperatur",
    }

    rename_overrides: Dict[str, str] = {
        "eb7000_fwe_zapfmenge_l_min": "EB7000 FWE Zapfmenge",
        "eb7000_fwe_eintritt_wärmetauscher": "EB7000 FWE Eintritt Wärmetauscher",
    }

    unit_overrides: Dict[str, str] = {
        "eb7000_fwe_zapfmenge_l_min": "l/min",
    }

    # Icon overrides for Home Assistant discovery
    icon_overrides: Dict[str, str] = {
        # Vorlauf / Rücklauf / Vorlaufanforderung / Speicher / Warmwasser / Kaltwasser
        "eb7000_hk1_vorlauftemperatur": "mdi:thermometer-water",
        "eb7000_hk1_ruecklauftemperatur": "mdi:thermometer-water",
        "eb7000_hk1_rücklauftemperatur": "mdi:thermometer-water",
        "eb7000_hk1_vorlaufanforderung": "mdi:thermometer-water",
        "eb7000_hk2_vorlauftemperatur": "mdi:thermometer-water",
        "eb7000_hk2_ruecklauftemperatur": "mdi:thermometer-water",
        "eb7000_hk2_rücklauftemperatur": "mdi:thermometer-water",
        "eb7000_hk2_vorlaufanforderung": "mdi:thermometer-water",
        "eb7000_hk3_vorlauftemperatur": "mdi:thermometer-water",
        "eb7000_hk3_ruecklauftemperatur": "mdi:thermometer-water",
        "eb7000_hk3_rücklauftemperatur": "mdi:thermometer-water",
        "eb7000_hk3_vorlaufanforderung": "mdi:thermometer-water",
        "eb7000_fwe_kaltwasser_zirkulation": "mdi:thermometer-water",
        "eb7000_fwe_warmwasser": "mdi:thermometer-water",
        "eb7000_fwe_eintritt_wärmetauscher": "mdi:thermometer-water",
        "eb7000_sp_fwe_niveau": "mdi:thermometer-water",
        "eb7000_sp_ht_niveau": "mdi:thermometer-water",
        "eb7000_sp_nt_niveau": "mdi:thermometer-water",
        "eb7000_sp_sp_unten": "mdi:thermometer-water",
        # Zapfmenge (flow)
        "eb7000_fwe_zapfmenge_l_min": "mdi:water-pump",
        # Betriebstemperatur + Außentemperatur
        "eb7000_wq_betriebstemperatur": "mdi:thermometer",
        "eb7000_ausstemperatur": "mdi:thermometer",
    }

    skip_unique_ids = {
        "eb7000_hk3_pause",
        "eb7000_hk2_pause",
        "eb7000_hk1_pause",
        "eb7000_ak_eb1000_count",
        "eb7000_ak_eb4000_count",
        "eb7000_ak_rbm8_count",
        "eb7000_ak_wp_exist",
        "eb7000_ak_wp_typ",
        "eb7000_hk2_mode",
        "eb7000_hk1_mode",
        "eb7000_hk1_name_control_hi",
        "eb7000_hk1_name_control_lo",
        "eb7000_hk1_status_bits_word0",
        "eb7000_hk1_status_bits_word8",
        "eb7000_hk2_name_control_hi",
        "eb7000_hk2_name_control_lo",
        "eb7000_hk3_mode",
        "eb7000_hk3_status_bits_word0",
        "eb7000_hk3_status_bits_word8",
        "eb7000_hk2_status_bits_word0",
        "eb7000_hk2_status_bits_word8",
        "eb7000_sk_kalttemperatur",
        "eb7000_sk_kollektortemperatur_f1",
        "eb7000_sk_leistung_kw",
        "eb7000_sk_name_control_hi",
        "eb7000_sk_name_control_lo",
        "eb7000_sk_nutztemperatur",
        "eb7000_sk_solardurchfluss",
        "eb7000_sk_warmtemperatur",
        "eb7000_sp_name_control_hi",
        "eb7000_sp_name_control_lo",
        "eb7000_wpint_name_control_hi",
        "eb7000_wpint_name_control_lo",
        "eb7000_wq_name_control_hi",
        "eb7000_wq_name_control_lo",
        "eb7000_fwe_mode",
        "eb7000_fwe_name_control_hi",
        "eb7000_fwe_name_control_lo",
    }

    def _slugify(part: str) -> str:
        s = part.strip().lower()
        out_chars: List[str] = []
        for ch in s:
            if ch.isalnum():
                out_chars.append(ch)
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
            # Build unique_id and a human-friendly name
            slug_parts = [_slugify(p) for p in path]
            unique_id = "eb7000_" + "_".join(slug_parts)

            # Skip unwanted sensors entirely
            if unique_id in skip_unique_ids:
                continue

            name = "EB7000 " + " / ".join(path)

            # Build Jinja2 value_template using dict-style access to be robust to umlauts
            path_expr = "".join(f'["{p}"]' for p in path)
            value_template = f"{{{{ value_json{path_expr} }}}}"

            payload = {
                "name": name,
                "state_topic": f"{base}/state",
                "value_template": value_template,
                "unique_id": unique_id,
                "device": device,
            }

            # Apply overrides for units and names
            if unique_id in temp_sensor_ids:
                payload["unit_of_measurement"] = "°C"
            if unique_id in unit_overrides:
                payload["unit_of_measurement"] = unit_overrides[unique_id]
            if unique_id in rename_overrides:
                payload["name"] = rename_overrides[unique_id]
            if unique_id in icon_overrides:
                payload["icon"] = icon_overrides[unique_id]

            _pub(f"sensor/{unique_id}/config", payload)

    # Explicit sensor for Außentemperatur
    aussentemp_sensor = {
        "sensor/eb7000_ausstemperatur/config": {
            "name": "EB7000 Außentemperatur",
            "state_topic": f"{base}/state",
            "unit_of_measurement": "°C",
            "value_template": "{{ value_json.sp.Außentemperatur }}",
            "unique_id": "eb7000_ausstemperatur",
            "icon": "mdi:thermometer",
            "device": device,
        }
    }

    for path, payload in aussentemp_sensor.items():
        _pub(path, payload)

    # Explicit sensors for HK1–HK3 Rücklauftemperatur
    rueck_sensors: Dict[str, Dict[str, Any]] = {
        "sensor/eb7000_hk1_ruecklauftemperatur/config": {
            "name": "EB7000 HK1 Rücklauftemperatur",
            "state_topic": f"{base}/state",
            "unit_of_measurement": "°C",
            "value_template": "{{ value_json.hk1.Rücklauftemperatur }}",
            "unique_id": "eb7000_hk1_ruecklauftemperatur",
            "icon": "mdi:thermometer-water",
            "device": device,
        },
        "sensor/eb7000_hk2_ruecklauftemperatur/config": {
            "name": "EB7000 HK2 Rücklauftemperatur",
            "state_topic": f"{base}/state",
            "unit_of_measurement": "°C",
            "value_template": "{{ value_json.hk2.Rücklauftemperatur }}",
            "unique_id": "eb7000_hk2_ruecklauftemperatur",
            "icon": "mdi:thermometer-water",
            "device": device,
        },
        "sensor/eb7000_hk3_ruecklauftemperatur/config": {
            "name": "EB7000 HK3 Rücklauftemperatur",
            "state_topic": f"{base}/state",
            "unit_of_measurement": "°C",
            "value_template": "{{ value_json.hk3.Rücklauftemperatur }}",
            "unique_id": "eb7000_hk3_ruecklauftemperatur",
            "icon": "mdi:thermometer-water",
            "device": device,
        },
    }

    for path, payload in rueck_sensors.items():
        _pub(path, payload)

    # Mode selects for HK1–HK3 and FWE
    selects: Dict[str, Dict[str, Any]] = {
        "select/eb7000_hk1_mode/config": {
            "name": "EB7000 HK1 Modus",
            "state_topic": f"{base}/hk1/mode_name",
            "command_topic": f"{base}/cmd/hk/1/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Party", "Frostschutz", "Urlaub", "Anheben"],
            "unique_id": "eb7000_hk1_mode",
            "device": device,
        },
        "select/eb7000_hk2_mode/config": {
            "name": "EB7000 HK2 Modus",
            "state_topic": f"{base}/hk2/mode_name",
            "command_topic": f"{base}/cmd/hk/2/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Party", "Frostschutz", "Urlaub", "Anheben"],
            "unique_id": "eb7000_hk2_mode",
            "device": device,
        },
        "select/eb7000_hk3_mode/config": {
            "name": "EB7000 HK3 Modus",
            "state_topic": f"{base}/hk3/mode_name",
            "command_topic": f"{base}/cmd/hk/3/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Party", "Frostschutz", "Urlaub", "Anheben"],
            "unique_id": "eb7000_hk3_mode",
            "device": device,
        },
        "select/eb7000_fwe_mode/config": {
            "name": "EB7000 FWE Modus",
            "state_topic": f"{base}/fwe/mode_name",
            "command_topic": f"{base}/cmd/fwe/mode",
            "value_template": "{{ value }}",
            "options": ["Automatik", "Spar"],
            "unique_id": "eb7000_fwe_mode",
            "device": device,
        },
    }

    for path, payload in selects.items():
        _pub(path, payload)

    # Urlaub days numbers for HK1–HK3
    numbers: Dict[str, Dict[str, Any]] = {
        "number/eb7000_hk1_urlaub_days/config": {
            "name": "EB7000 HK1 Urlaubstage",
            "state_topic": f"{base}/hk1/urlaub_days",
            "command_topic": f"{base}/cmd/hk/1/urlaub_days",
            "min": 1,
            "max": 365,
            "step": 1,
            "mode": "box",
            "unit_of_measurement": "d",
            "unique_id": "eb7000_hk1_urlaub_days",
            "device": device,
        },
        "number/eb7000_hk2_urlaub_days/config": {
            "name": "EB7000 HK2 Urlaubstage",
            "state_topic": f"{base}/hk2/urlaub_days",
            "command_topic": f"{base}/cmd/hk/2/urlaub_days",
            "min": 1,
            "max": 365,
            "step": 1,
            "mode": "box",
            "unit_of_measurement": "d",
            "unique_id": "eb7000_hk2_urlaub_days",
            "device": device,
        },
        "number/eb7000_hk3_urlaub_days/config": {
            "name": "EB7000 HK3 Urlaubstage",
            "state_topic": f"{base}/hk3/urlaub_days",
            "command_topic": f"{base}/cmd/hk/3/urlaub_days",
            "min": 1,
            "max": 365,
            "step": 1,
            "mode": "box",
            "unit_of_measurement": "d",
            "unique_id": "eb7000_hk3_urlaub_days",
            "device": device,
        },
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

