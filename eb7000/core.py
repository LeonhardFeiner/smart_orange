"""
Core Modbus/Web-UI logic for the EB7000 heating controller.

This is a copy of the implementation originally developed in
`read_web_ui_values.py`, without the CLI entrypoint.
"""

import socket
import struct
from typing import Any, List, Optional, Dict

# === Configuration ===
HOST = "192.168.0.9"
PORT = 502
SLAVE_ID = 80

# EB7000 object start addresses (hex), from boot.js ConstEB7000Addresses
# HK1, HK2, FWE, SK, WQ, SP, WPint, WPsiem
CONST_EB7000_ADDRESSES = ["2800", "3000", "3800", "4000", "4800", "5000", "B800", "C000"]
# EB1000 object types: AK address 9+i gives type for i-th EB1000
CONST_TYPE50_IDENT = ["RBM8", "RBM8F", "None", "HK", "FWE", "SK", "WQ", "WPint", "WPsiem"]
CONST_MODBUS_HEADER_LENGTH = 18  # hex chars = 9 bytes (matches common.js)


def _build_modbus_fc03(unit: int, start_addr: int, count: int) -> bytes:
    tid, pid = 1, 0
    pdu = bytes([unit, 0x03]) + struct.pack(">HH", start_addr, count)
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def _build_modbus_fc04(unit: int, start_addr: int, count: int) -> bytes:
    tid, pid = 1, 0
    pdu = bytes([unit, 0x04]) + struct.pack(">HH", start_addr, count)
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def _build_modbus_fc4a(unit: int, start_addr: int) -> bytes:
    tid, pid = 1, 0
    pdu = bytes([unit, 0x4A]) + struct.pack(">H", start_addr)
    length = len(pdu)
    return struct.pack(">HHH", tid, pid, length) + pdu


def _build_modbus_fc06(unit: int, reg_addr: int, value: int) -> bytes:
    tid, pid = 1, 0
    pdu = bytes([unit, 0x06]) + struct.pack(">HH", reg_addr, value & 0xFFFF)
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def _build_modbus_fc16(unit: int, start_addr: int, values: List[int]) -> bytes:
    tid, pid = 1, 0
    data = b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
    pdu = bytes([unit, 0x10]) + struct.pack(">HHB", start_addr, len(values), len(data)) + data
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def _send_modbus(cmd: bytes, host: str = HOST, port: int = PORT, timeout: float = 2.0) -> Optional[bytes]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall(cmd)
        resp = s.recv(4096)
        s.close()
        return resp
    except Exception:
        return None


def _parse_fc03_response(resp: bytes) -> Optional[List[int]]:
    if len(resp) < 10 or resp[7] != 0x03:
        return None
    byte_count = resp[8]
    data = resp[9 : 9 + byte_count]
    vals = []
    for i in range(0, len(data), 2):
        if i + 2 <= len(data):
            vals.append(struct.unpack(">H", data[i : i + 2])[0])
    return vals


def _parse_fc04_response(resp: bytes) -> Optional[List[int]]:
    if len(resp) < 10 or resp[7] != 0x04:
        return None
    byte_count = resp[8]
    data = resp[9 : 9 + byte_count]
    vals = []
    for i in range(0, len(data), 2):
        if i + 2 <= len(data):
            vals.append(struct.unpack(">H", data[i : i + 2])[0])
    return vals


def _response_to_words(resp: bytes) -> Optional[List[int]]:
    if len(resp) < 9:
        return None
    fc = resp[7]
    if fc == 0x03:
        return _parse_fc03_response(resp)
    if fc == 0x04:
        return _parse_fc04_response(resp)
    if fc == 0x4A:
        byte_count = resp[8]
        data = resp[9 : 9 + byte_count]
        return [struct.unpack(">H", data[i : i + 2])[0] for i in range(0, len(data), 2) if i + 2 <= len(data)]
    return None


def get_modbus_dec(data: List[int], start_addr: int, length: int, signed: bool = True) -> Optional[int]:
    if start_addr + length > len(data):
        return None
    val = data[start_addr]
    if length > 1:
        val = sum(data[start_addr + i] * (65536**i) for i in range(length))
    if signed and val > 32767:
        val = val - 65536
    return val


def get_modbus_bits(data: List[int], word_idx: int, word_len: int, needle_start: int, needle_len: int) -> int:
    if word_idx + word_len > len(data):
        return 0
    bits = ""
    for w in data[word_idx : word_idx + word_len]:
        b = format(w & 0xFFFF, "016b")[::-1]
        bits += b
    if needle_start + needle_len > len(bits):
        return 0
    return int(bits[needle_start : needle_start + needle_len][::-1], 2)


def read_ak_parameter(host: str = HOST, port: int = PORT) -> Dict[str, Any]:
    cmd = _build_modbus_fc03(80, 0x2000, 41)
    resp = _send_modbus(cmd, host, port)
    words = _response_to_words(resp) if resp else []
    if not words:
        return {"error": "no response", "raw": None}

    return {
        "rbm8_count": get_modbus_dec(words, 6, 1, signed=False),
        "eb1000_count": get_modbus_dec(words, 7, 1, signed=False),
        "eb4000_count": get_modbus_dec(words, 8, 1, signed=False),
        "wp_exist": get_modbus_dec(words, 39, 1, signed=False),
        "wp_typ": get_modbus_dec(words, 40, 1, signed=False),
        "raw_words": words,
    }


def read_actual_values_fc4a(unit_hex: str, start_addr_hex: str, label: str, host: str = HOST, port: int = PORT) -> Dict[str, Any]:
    unit = int(unit_hex, 16)
    start_addr = int(start_addr_hex, 16)
    cmd = _build_modbus_fc4a(unit, start_addr)
    resp = _send_modbus(cmd, host, port)
    words = _response_to_words(resp) if resp else []
    if not words:
        return {"error": "no response", "raw": None}
    return {"label": label, "words": words, "raw": words}


def read_actual_values_fc03(unit: int, start_addr: int, count: int, host: str = HOST, port: int = PORT) -> Optional[List[int]]:
    cmd = _build_modbus_fc03(unit, start_addr, count)
    resp = _send_modbus(cmd, host, port)
    return _response_to_words(resp)


def read_actual_values_fc04(unit: int, start_addr: int, count: int, host: str = HOST, port: int = PORT) -> Optional[List[int]]:
    cmd = _build_modbus_fc04(unit, start_addr, count)
    resp = _send_modbus(cmd, host, port)
    return _response_to_words(resp)


def _build_modbus_fc4c_fwe(unit: int, start_addr: int, mode: int) -> bytes:
    tid, pid = 1, 0
    pdu = bytes([unit, 0x4C]) + struct.pack(">H", start_addr) + bytes([0x00, 0x01, 0x02, 0x00, mode & 0xFF])
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def _build_modbus_fc4c_hk(unit: int, start_addr: int, mode: int, urlaub_days: Optional[int] = None) -> bytes:
    tid, pid = 1, 0
    if mode == 3 and urlaub_days is not None:
        days_hex = struct.pack(">H", urlaub_days)
        pdu = bytes([unit, 0x4C]) + struct.pack(">H", start_addr) + bytes([0x00, 0x02, 0x04, 0x00, 0x03]) + days_hex
    else:
        pdu = bytes([unit, 0x4C]) + struct.pack(">H", start_addr) + bytes([0x00, 0x02, 0x04, 0x00, mode & 0xFF, 0x00, 0x00])
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def set_fwe_mode(mode: int, host: str = HOST, port: int = PORT) -> bool:
    cmd = _build_modbus_fc4c_fwe(80, 0x3800, mode)
    resp = _send_modbus(cmd, host, port)
    # Success response echoes function code 0x4C. Exception would be 0xCC.
    return resp is not None and len(resp) >= 8 and resp[7] == 0x4C


def set_hk_mode(hk: int, mode: int, urlaub_days: Optional[int] = None, host: str = HOST, port: int = PORT) -> bool:
    addrs = [(80, 0x2800), (80, 0x3000), (17, 0x2000)]
    if hk < 1 or hk > len(addrs):
        return False
    unit, start = addrs[hk - 1]
    cmd = _build_modbus_fc4c_hk(unit, start, mode, urlaub_days)
    resp = _send_modbus(cmd, host, port)
    return resp is not None and len(resp) >= 8 and resp[7] == 0x4C


def set_fwe_mode_std(mode: int, host: str = HOST, port: int = PORT) -> bool:
    cmd = _build_modbus_fc06(80, 0x3801, mode)
    resp = _send_modbus(cmd, host, port)
    return resp is not None and len(resp) >= 8 and resp[7] == 0x06


def set_hk_mode_std(hk: int, mode: int, urlaub_days: Optional[int] = None, host: str = HOST, port: int = PORT) -> bool:
    addrs = [(80, 0x2800), (80, 0x3000), (17, 0x2000)]
    if hk < 1 or hk > len(addrs):
        return False
    unit, start = addrs[hk - 1]
    if mode == 3 and urlaub_days is not None:
        cmd = _build_modbus_fc16(unit, start + 1, [3, urlaub_days])
        resp = _send_modbus(cmd, host, port)
        return resp is not None and len(resp) >= 8 and resp[7] == 0x10
    else:
        cmd = _build_modbus_fc06(unit, start + 1, mode)
        resp = _send_modbus(cmd, host, port)
        return resp is not None and len(resp) >= 8 and resp[7] == 0x06


def build_hk_urlaub_fc4c_hex(hk: int, urlaub_days: int) -> Optional[str]:
    """
    Build the raw FC4C Modbus frame (in hex) used by the original web UI
    when setting Urlaub for a given heating circuit.

    This is intended for debugging/logging purposes only.
    """
    addrs = {1: ("50", "2800"), 2: ("50", "3000"), 3: ("11", "2000")}
    if hk not in addrs:
        return None
    unit_hex, addr_hex = addrs[hk]
    days_hex = f"{urlaub_days:04x}"
    return f"00010000000b{unit_hex}4c{addr_hex}0002040003{days_hex}"


def extract_sp_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 11:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["FWE_Niveau", "HT_Niveau", "NT_Niveau", "SP_unten"]):
        v = get_modbus_dec(words, 2 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    out = get_modbus_dec(words, 10, 1)
    r["Außentemperatur"] = (out / 10.0) if out is not None and out not in (3150, -3150, 31500, -31500) else None
    return r


HK_MODE_NAMES = {0: "Automatik", 1: "Party", 2: "Frostschutz", 3: "Urlaub", 4: "Anheben"}
FWE_MODE_NAMES = {0: "Automatik", 1: "Spar"}

OBJECT_FRIENDLY_NAMES = {
    "ak": "Anlagenkonfiguration",
    "hk1": "Heizkreis 1",
    "hk2": "Heizkreis 2",
    "fwe": "Frischwasser 1",
    "sk": "Solarkreis",
    "wq": "Wärmequelle",
    "sp": "Speicher 1",
    "wpint": "Wärmepumpe intern",
    "wpsiem": "Wärmepumpe Siemens",
    "wp4000": "Wärmepumpe EB4000",
}


def get_modbus_string(data: List[int], start_addr: int, length: int) -> str:
    if start_addr >= len(data):
        return ""
    words_available = len(data) - start_addr
    words_to_use = min(length + 1, words_available)
    raw = b"".join(struct.pack("<H", data[start_addr + i] & 0xFFFF) for i in range(words_to_use))
    try:
        s = raw.decode("latin-1", errors="ignore")
    except Exception:
        return ""
    if s:
        s = s[1:]
    s = s.rstrip("\x00 ").strip()
    while s and not (s[0].isalpha() or s[0] in "ÄÖÜäöüß"):
        s = s[1:]
    return s


def extract_hk_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 8:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Vorlauftemperatur", "Rücklauftemperatur", "Vorlaufanforderung"]):
        v = get_modbus_dec(words, 3 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    r["Pause"] = get_modbus_dec(words, 7, 1, signed=False)
    r["status_bits_word0"] = get_modbus_dec(words, 0, 1, signed=False)
    r["status_bits_word8"] = get_modbus_dec(words, 8, 1, signed=False)
    mode = get_modbus_dec(words, 1, 1, signed=False)
    r["mode"] = mode
    r["mode_name"] = HK_MODE_NAMES.get(mode, f"unknown({mode})") if mode is not None else None
    return r


def extract_fwe_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 7:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    v3 = get_modbus_dec(words, 3, 1)
    v4 = get_modbus_dec(words, 4, 1)
    v5 = get_modbus_dec(words, 5, 1)
    v6 = get_modbus_dec(words, 6, 1)
    r["Kaltwasser_&_Zirkulation"] = (v3 / 10.0) if v3 is not None and v3 not in (3150, -3150) else None
    r["Warmwasser"] = (v4 / 10.0) if v4 is not None and v4 not in (3150, -3150) else None
    r["Eintritt_Wärmetauscher"] = (v5 / 10.0) if v5 is not None and v5 not in (3150, -3150) else None
    r["Zapfmenge"] = (v6 / 100.0) if v6 is not None and v6 not in (3150, -3150) else None
    mode = get_modbus_dec(words, 1, 1, signed=False)
    r["mode"] = mode
    r["mode_name"] = FWE_MODE_NAMES.get(mode, f"unknown({mode})") if mode is not None else None
    return r


def extract_sk_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 9:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(
        ["Kollektortemperatur_F1", "Warmtemperatur", "Kalttemperatur", "Nutztemperatur", "Solardurchfluss"]
    ):
        v = get_modbus_dec(words, 1 + i, 1)
        if i == 4:
            r[key] = (v / 100.0) if v is not None else None
        else:
            r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    low = get_modbus_dec(words, 7, 1, signed=False) or 0
    high = get_modbus_dec(words, 8, 1, signed=False) or 0
    r["Leistung_kW"] = (low + high * 65535) / 1000.0
    return r


def extract_wq_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 3:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Betriebstemperatur", "Rücklauftemperatur"]):
        v = get_modbus_dec(words, 1 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    return r


def extract_wpint_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 5:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Vorlauftemperatur", "SoleKalt", "KühlspeicherOben", "KühlspeicherUnten"]):
        v = get_modbus_dec(words, 1 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    return r


def extract_wpsiem_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 10:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Vorlauftemperatur", "Rücklauftemperatur", "Quellenaustritt", "Quelleneintritt", "KühlspeicherUnten"]):
        v = get_modbus_dec(words, 5 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    return r


def extract_wp4000_values(words: List[int]) -> Dict[str, Any]:
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Abtau", "QuellenEIN", "QuellenAUS"]):
        if len(words) > 12 + i:
            v = get_modbus_dec(words, 12 + i, 1)
            r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
        else:
            r[key] = None
    for i, key in enumerate(["WP_Vorlauf", "WP_Rücklauf"]):
        if len(words) > 21 + i:
            v = get_modbus_dec(words, 21 + i, 1)
            r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
        else:
            r[key] = None
    return r


def detect_present_objects(host: str = HOST, port: int = PORT) -> set:
    """
    Return the set of object keys that are physically present on the EB7000.

    Mirrors the detection logic of the web UI (configuration.js fetchAkObjects):
    - Reads AK parameter for counts and heat-pump type.
    - For each of the 8 fixed EB7000 addresses, reads 16 config words via FC03.
      Word 6 (myStatus) must be non-zero for the object to be considered present.
    - WPint requires ak.wp_exist==1 and ak.wp_typ==1.
    - WPsiem requires ak.wp_exist==1 and ak.wp_typ==2.
    - EB1000 extension modules of type "HK" become hk3, hk4, ...
    - EB4000 modules become wp4000.
    """
    ak = read_ak_parameter(host, port)
    if "error" in ak:
        return set()

    wp_exist = ak.get("wp_exist") or 0
    wp_typ = ak.get("wp_typ") or 0
    eb1000_count = ak.get("eb1000_count") or 0
    eb4000_count = ak.get("eb4000_count") or 0
    ak_words: List[int] = ak.get("raw_words") or []

    # Fixed EB7000 objects in order (index = EB7000AKStep 0-7 in web UI)
    fixed = [
        ("hk1",    0x50, 0x2800),
        ("hk2",    0x50, 0x3000),
        ("fwe",    0x50, 0x3800),
        ("sk",     0x50, 0x4000),
        ("wq",     0x50, 0x4800),
        ("sp",     0x50, 0x5000),
        ("wpint",  0x50, 0xB800),
        ("wpsiem", 0x50, 0xC000),
    ]

    present: set = set()

    for obj_key, unit, addr in fixed:
        cfg_words = read_actual_values_fc03(unit, addr, 16, host, port)
        if not cfg_words or len(cfg_words) < 7:
            continue
        status = cfg_words[6]  # myStatus in web UI — non-zero means installed
        if status == 0:
            continue
        if obj_key == "wpint" and (wp_exist != 1 or wp_typ != 1):
            continue
        if obj_key == "wpsiem" and (wp_exist != 1 or wp_typ != 2):
            continue
        present.add(obj_key)

    # EB1000 extension modules (units 17+i, address 0x2000)
    hk_ext_count = 0
    for i in range(eb1000_count):
        type_idx = get_modbus_dec(ak_words, 9 + i, 1, signed=False) if len(ak_words) > 9 + i else None
        if type_idx is None:
            continue
        type_name = CONST_TYPE50_IDENT[type_idx] if 0 <= type_idx < len(CONST_TYPE50_IDENT) else "None"
        if type_name == "HK":
            present.add(f"hk{3 + hk_ext_count}")
            hk_ext_count += 1
        # Other EB1000 types (FWE, SK, WQ, WPint, WPsiem) share keys with
        # their base counterparts and are currently handled by the same extractors.

    # EB4000 heat-pump expansion
    if eb4000_count > 0:
        present.add("wp4000")

    return present


def read_all_web_ui_values(host: str = HOST, port: int = PORT, use_standard_modbus: bool = False) -> Dict[str, Any]:
    ak = read_ak_parameter(host, port)
    result: Dict[str, Any] = {
        "ak": ak,
        "hk1": {},
        "hk2": {},
        "fwe": {},
        "sk": {},
        "wq": {},
        "sp": {},
        "wpint": {},
        "wpsiem": {},
        "wp4000": {},
    }

    if isinstance(result["ak"], dict):
        result["ak"].setdefault("name", OBJECT_FRIENDLY_NAMES.get("ak", "Anlagenkonfiguration"))

    objects = [
        ("hk1", "50", "2800", extract_hk_values),
        ("hk2", "50", "3000", extract_hk_values),
        ("fwe", "50", "3800", extract_fwe_values),
        ("sk", "50", "4000", extract_sk_values),
        ("wq", "50", "4800", extract_wq_values),
        ("sp", "50", "5000", extract_sp_values),
        ("wpint", "50", "B800", extract_wpint_values),
        ("wpsiem", "50", "C000", extract_wpsiem_values),
    ]

    for key, unit, addr, extractor in objects:
        start = int(addr, 16)
        unit_int = int(unit, 16)
        if use_standard_modbus:
            words = read_actual_values_fc04(unit_int, start, 16, host, port)
            if not words:
                words = read_actual_values_fc03(unit_int, start, 16, host, port)
            if words:
                result[key] = extractor(words)
                result[key]["raw_words"] = words
            else:
                result[key] = {"error": "no response"}
        else:
            data = read_actual_values_fc4a(unit, addr, key, host, port)
            if "error" in data:
                result[key] = {"error": data["error"]}
                words = read_actual_values_fc03(unit_int, start, 16, host, port)
                if words:
                    result[key] = extractor(words)
                continue
            words = data.get("words", [])
            result[key] = extractor(words) if words else {"error": "empty"}

        if isinstance(result.get(key), dict):
            base_name = OBJECT_FRIENDLY_NAMES.get(key, key)
            cfg_words = read_actual_values_fc03(unit_int, start, 16, host, port)
            obj_name = get_modbus_string(cfg_words, 0, 5) if cfg_words else ""
            if cfg_words:
                w0 = cfg_words[0] & 0xFFFF
                hi = (w0 >> 8) & 0xFF
                lo = w0 & 0xFF
                result[key]["name_control_hi"] = hi
                result[key]["name_control_lo"] = lo
            if obj_name:
                result[key].setdefault("device_name", obj_name)
                result[key]["name"] = f"{base_name} - {obj_name}"
            else:
                result[key].setdefault("name", base_name)

    eb1000_count = ak.get("eb1000_count", 0) or 0
    ak_words = ak.get("raw_words") if isinstance(ak.get("raw_words"), list) else []
    hk_ext_count = 0  # counts only HK-type EB1000 modules for consecutive hk3/hk4/... numbering
    for i in range(eb1000_count):
        obj_type_idx = get_modbus_dec(ak_words, 9 + i, 1, signed=False) if len(ak_words) > 9 + i else None
        obj_type = CONST_TYPE50_IDENT[obj_type_idx] if obj_type_idx is not None and 0 <= obj_type_idx < len(CONST_TYPE50_IDENT) else "None"
        if obj_type == "HK":
            hk_key = f"hk{3 + hk_ext_count}"
            hk_ext_count += 1
            result[hk_key] = {}
            unit_eb = 17 + i
            cfg_words_eb = read_actual_values_fc03(unit_eb, 0x2000, 16, host, port)
            hk_device_name = get_modbus_string(cfg_words_eb, 0, 5) if cfg_words_eb else ""
            if use_standard_modbus:
                words = read_actual_values_fc04(unit_eb, 0x2000, 16, host, port)
                if not words:
                    words = read_actual_values_fc03(unit_eb, 0x2000, 16, host, port)
                if words:
                    result[hk_key] = extract_hk_values(words)
                    result[hk_key]["raw_words"] = words
                else:
                    result[hk_key] = {"error": "no response"}
            else:
                data = read_actual_values_fc4a(f"{unit_eb:02x}", "2000", hk_key, host, port)
                if "error" not in data and data.get("words"):
                    result[hk_key] = extract_hk_values(data["words"])
                else:
                    words_fc03 = read_actual_values_fc03(unit_eb, 0x2000, 16, host, port)
                    result[hk_key] = extract_hk_values(words_fc03) if words_fc03 else {"error": "no response"}

            if isinstance(result.get(hk_key), dict):
                try:
                    hk_num = int(hk_key[2:])
                    base_name = f"Heizkreis {hk_num}"
                    if hk_device_name:
                        result[hk_key].setdefault("device_name", hk_device_name)
                        result[hk_key]["name"] = f"{base_name} - {hk_device_name}"
                    else:
                        result[hk_key].setdefault("name", base_name)
                except ValueError:
                    result[hk_key].setdefault("name", hk_key)

    ak_cfg = result.get("ak", {})
    if ak_cfg.get("eb4000_count", 0) > 0:
        unit = 33
        words = read_actual_values_fc03(unit, 0x2000, 24, host, port)
        result["wp4000"] = extract_wp4000_values(words) if words else {"error": "no response"}
        if isinstance(result["wp4000"], dict):
            result["wp4000"].setdefault("name", OBJECT_FRIENDLY_NAMES.get("wp4000", "Wärmepumpe EB4000"))

    return result


__all__ = [
    "read_all_web_ui_values",
    "detect_present_objects",
    "set_hk_mode",
    "set_hk_mode_std",
    "set_fwe_mode",
    "build_hk_urlaub_fc4c_hex",
    "extract_wpint_values",
    "extract_wpsiem_values",
    "extract_wp4000_values",
]


