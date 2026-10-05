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
    if not resp or len(resp) < 10 or resp[7] != 0x03:
        return None
    byte_count = resp[8]
    data = resp[9 : 9 + byte_count]
    vals = []
    for i in range(0, len(data), 2):
        if i + 2 <= len(data):
            vals.append(struct.unpack(">H", data[i : i + 2])[0])
    return vals


def _parse_fc04_response(resp: bytes) -> Optional[List[int]]:
    if not resp or len(resp) < 10 or resp[7] != 0x04:
        return None
    byte_count = resp[8]
    data = resp[9 : 9 + byte_count]
    vals = []
    for i in range(0, len(data), 2):
        if i + 2 <= len(data):
            vals.append(struct.unpack(">H", data[i : i + 2])[0])
    return vals


def _response_to_words(resp: Optional[bytes]) -> Optional[List[int]]:
    if not resp or len(resp) < 9:
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


def _build_modbus_fc06(unit: int, reg_addr: int, value: int) -> bytes:
    tid, pid = 1, 0
    pdu = bytes([unit, 0x06]) + struct.pack(">HH", reg_addr, value & 0xFFFF)
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def _build_modbus_fc10(unit: int, reg_addr: int, values: List[int]) -> bytes:
    tid, pid = 1, 0
    data = b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
    pdu = bytes([unit, 0x10]) + struct.pack(">HHB", reg_addr, len(values), len(data)) + data
    return struct.pack(">HHH", tid, pid, len(pdu)) + pdu


def set_fwe_temp(temp_celsius: float, spar: bool = False, host: str = HOST, port: int = PORT) -> bool:
    """
    Set FWE (Warmwasser) target temperature in °C (valid range 35.0 - 75.0 °C).
    spar=False: sets WWNormalSollTemperatur (reg 0x3809)
    spar=True:  sets WWSparSollTemperatur (reg 0x380A)
    """
    if not (35.0 <= temp_celsius <= 75.0):
        return False
    val_int = int(round(temp_celsius * 10.0))
    reg_addr = 0x380A if spar else 0x3809
    cmd = _build_modbus_fc10(80, reg_addr, [val_int])
    resp = _send_modbus(cmd, host, port)
    return resp is not None and len(resp) >= 8 and resp[7] == 0x10


def set_wpsiem_mode(mode: int, host: str = HOST, port: int = PORT) -> bool:
    """
    Set Siemens heat pump mode.
    0: Automatik (FC 0x4C)
    1: ManuellAus (FC 0x4C)
    2: ManuellRESET (FC 0x06, unit 0x96=150, reg 0x5043=20547, val 1)
    """
    if mode == 2:
        cmd = _build_modbus_fc06(150, 0x5043, 1)
        resp = _send_modbus(cmd, host, port)
        return resp is not None and len(resp) >= 8 and resp[7] == 0x06
    elif mode in (0, 1):
        cmd = _build_modbus_fc4c_fwe(80, 0xC000, mode)
        resp = _send_modbus(cmd, host, port)
        return resp is not None and len(resp) >= 8 and resp[7] == 0x4C
    return False


def set_wp4000_mode(mode: int, host: str = HOST, port: int = PORT) -> bool:
    """
    Set EB4000 heat pump mode.
    0: Automatik
    1: Silent
    2: ManuellAus
    6: ManuellRESET
    All sent via FC 0x4C to unit 33 (0x21), start address 0x2000.
    """
    if mode in (0, 1, 2, 6):
        cmd = _build_modbus_fc4c_fwe(33, 0x2000, mode)
        resp = _send_modbus(cmd, host, port)
        return resp is not None and len(resp) >= 8 and resp[7] == 0x4C
    return False



def extract_sp_status_meldung(w0: int, w1: int) -> str:
    """Exact reproduction of EB7000 firmware refreshSPMessages (gui_sp.js)."""
    if w0 & 0x0001:
        return "Fehler: EEPROM"
    if w0 & 0x0002:
        return "Fehler: Parameter"
    if w0 & 0x0008:
        return "Fehler: Sensoren überprüfen"

    if w0 & 0x1000:
        return "WW Vorrang"
    if w1 & 0x0001:
        return "Wärmeanforderung FWE"
    if w1 & 0x0002:
        return "Wärmeanforderung FWE erweitert"
    if w1 & 0x0004:
        return "Wärmeanforderung HT-HK"
    if w1 & 0x0008:
        return "Wärmeanforderung HT-HK erweitert"
    if w1 & 0x0010:
        return "Wärmeanforderung NT-HK"
    if w0 & 0x8000:
        return "Maximale Speicher Temperatur (S10) erreicht"
    return "Bereit"


def extract_sp_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 11:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["FWE_Niveau", "HT_Niveau", "NT_Niveau", "SP_unten"]):
        v = get_modbus_dec(words, 2 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    out = get_modbus_dec(words, 10, 1)
    r["Außentemperatur"] = (out / 10.0) if out is not None and out not in (3150, -3150, 31500, -31500) else None

    w0 = get_modbus_dec(words, 0, 1, signed=False) or 0
    w1 = get_modbus_dec(words, 1, 1, signed=False) or 0
    r["status_bits_word0"] = w0
    r["status_bits_word1"] = w1
    r["Statusmeldung"] = extract_sp_status_meldung(w0, w1)
    r["WW_Vorrang"] = bool(w0 & 0x1000)
    r["Waermeanforderung_FWE"] = bool(w1 & 0x0001)
    r["Waermeanforderung_FWE_Erweitert"] = bool(w1 & 0x0002)
    r["Waermeanforderung_HT_HK"] = bool(w1 & 0x0004)
    r["Waermeanforderung_HT_HK_Erweitert"] = bool(w1 & 0x0008)
    r["Waermeanforderung_NT_HK"] = bool(w1 & 0x0010)
    r["Max_Speichertemperatur_Erreicht"] = bool(w0 & 0x8000)
    r["Fehler"] = bool(w0 & 0x000B)
    r["Fehler_EEPROM"] = bool(w0 & 0x0001)
    r["Fehler_Parameter"] = bool(w0 & 0x0002)
    r["Fehler_Sensoren"] = bool(w0 & 0x0008)
    return r


HK_MODE_NAMES = {0: "Automatik", 1: "Party", 2: "Frostschutz", 3: "Urlaub", 4: "Anheben"}
FWE_MODE_NAMES = {0: "Automatik", 1: "Spar"}
WPSIEM_MODE_NAMES = {0: "Automatik", 1: "ManuellAus", 2: "ManuellRESET"}
WP4000_MODE_NAMES = {0: "Automatik", 1: "Silent", 2: "ManuellAus", 6: "ManuellRESET"}

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


def extract_hk_status_meldung(w0: int, mode: int, pause: int, w8: int) -> str:
    """Exact reproduction of EB7000 firmware MessageHK (gui_hk.js:refreshHKMessages)."""
    # Fehlerüberprüfung (Bits 0-3)
    if w0 & 0x0001:
        return "Fehler: EEPROM"
    if w0 & 0x0002:
        return "Fehler: Parameter"
    if w0 & 0x0004:
        return "Fehler: Programm"
    if w0 & 0x0008:
        return "Fehler: Sensoren überprüfen"

    # HW Freigabe (Bit 6)
    if not (w0 & 0x0040):
        return "HK extern blockiert"

    # Trockenheizen (Bits 10-12)
    trocken = (w0 >> 10) & 0x07
    if trocken == 1:
        return "Trockenheizen: Sockel"
    elif trocken == 2:
        return "Trockenheizen: Anstieg"
    elif trocken == 3:
        return "Trockenheizen: Scheitel"
    elif trocken == 4:
        return "Trockenheizen: Abfall"

    # Word 8 Sonderfunktionen
    if w8 & 0x0004:
        return "Sommerkick aktiv"
    if w8 & 0x0080:
        return "Kühlen aktiv"
    if (not (w8 & 0x0080)) and (w8 & 0x0020):
        return "Kühlen Pause"
    if w8 & 0x0002:
        return "Raumaktivierung"
    if w8 & 0x0001:
        return "Heizbetrieb: SpeicherWärmeÜberschuss"

    # BetriebsModus (Bit 15): 0 = Winter, 1 = Sommer
    betriebsmodus = (w0 >> 15) & 0x01
    prog_map = {0: "Frostschutz", 1: "Spar", 2: "Normal", 3: "Schnellaufheizung"}
    pz = (w0 >> 8) & 0x03
    pz_name = prog_map.get(pz, "Normal")

    if betriebsmodus == 1:
        frost_hw = (w0 >> 13) & 0x03
        if frost_hw == 2:
            return "Frostschutz aktiv"
        elif mode == 2:
            return "Abschaltung aufgrund Betriebsart Frostschutz"
        else:
            return "Abschaltung aufgrund Aussentemperatur"
    else:
        # Wintermodus
        if pause > 0:
            return f"Zeitprogramm: {pz_name}"
        else:
            return f"Heizkreis in Pause (Zeitprogramm: {pz_name})"


def extract_hk_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 8:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Vorlauftemperatur", "Rücklauftemperatur", "Vorlaufanforderung"]):
        v = get_modbus_dec(words, 3 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    
    pause = get_modbus_dec(words, 7, 1, signed=False) or 0
    w0 = get_modbus_dec(words, 0, 1, signed=False) or 0
    w8 = get_modbus_dec(words, 8, 1, signed=False) or 0
    mode = get_modbus_dec(words, 1, 1, signed=False) or 0

    v9 = get_modbus_dec(words, 9, 1) if len(words) > 9 else None
    r["Modul_Aussentemperatur"] = (v9 / 10.0) if v9 is not None and v9 not in (3150, -3150, 31500, -31500) else None

    r["Pause"] = pause
    r["status_bits_word0"] = w0
    r["status_bits_word8"] = w8
    r["mode"] = mode
    r["mode_name"] = HK_MODE_NAMES.get(mode, f"unknown({mode})")

    # Decoded status message
    meldung = extract_hk_status_meldung(w0, mode, pause, w8)
    r["Statusmeldung"] = meldung

    # Sommerabschaltung is active when in Sommermodus and not in hardware-frostschutz or frostschutz mode
    betriebsmodus = (w0 >> 15) & 0x01
    sommerabschaltung = bool(betriebsmodus == 1 and mode != 2 and ((w0 >> 13) & 0x03) != 2)
    r["Sommerabschaltung"] = sommerabschaltung
    r["Betriebsmodus"] = "Sommer" if betriebsmodus == 1 else "Winter"

    prog_map = {0: "Frostschutz", 1: "Spar", 2: "Normal", 3: "Schnellaufheizung"}
    r["Programmzustand"] = prog_map.get((w0 >> 8) & 0x03, "Normal")

    r["HW_Freigabe"] = bool(w0 & 0x0040)
    r["Pause_Aktiv"] = bool(pause == 0)
    r["Fehler"] = bool(w0 & 0x000F)
    r["Fehler_EEPROM"] = bool(w0 & 0x0001)
    r["Fehler_Parameter"] = bool(w0 & 0x0002)
    r["Fehler_Programm"] = bool(w0 & 0x0004)
    r["Fehler_Sensoren"] = bool(w0 & 0x0008)

    # Sonderbetriebsarten
    r["Sommerkick"] = bool(w8 & 0x0004)
    r["Kuehlen_Aktiv"] = bool(w8 & 0x0080)
    r["Kuehlen_Pause"] = bool((not (w8 & 0x0080)) and (w8 & 0x0020))
    r["Raumaktivierung"] = bool(w8 & 0x0002)
    r["Speicher_Waermeueberschuss"] = bool(w8 & 0x0001)

    trocken_map = {0: "Aus", 1: "Sockel", 2: "Anstieg", 3: "Scheitel", 4: "Abfall"}
    r["Trockenheizen"] = trocken_map.get((w0 >> 10) & 0x07, "Aus")
    r["Frostschutz_Aktiv"] = bool(((w0 >> 13) & 0x03) == 2)

    return r


def extract_fwe_status_meldung(w0: int, w1: int) -> str:
    """Exact reproduction of EB7000 firmware RefreshFWEMessages (gui_fwe.js)."""
    # Fehlerüberprüfung
    if w0 & 0x0001:
        return "Fehler: EEPROM"
    if w0 & 0x0002:
        return "Fehler: Parameter"
    if w0 & 0x0004:
        return "Fehler: Programm"
    if w0 & 0x0008:
        return "Fehler: Sensoren überprüfen"
    if w0 & 0x0010:
        return "Kein Durchfluss bei Zirkubetrieb"

    # Meldung wenn kein Fehler
    if w0 & 0x0020:  # Zirkulation aktiv
        if ((w0 >> 13) & 0x07) == 4:
            return "Tauscherabkühlung"
        return "Zirkulation aktiv"

    betriebsart = w1 & 0x01
    prg_zustand = (w0 >> 8) & 0x03
    zirk_zustand = (w0 >> 10) & 0x01
    zirku_text = "frei" if zirk_zustand == 1 else "Gesperrt"

    if betriebsart == 0:  # AUTOMATIK
        if prg_zustand == 1:
            return f"Zeitprogramm: Spar / Zirku: {zirku_text}"
        elif prg_zustand == 2:
            return f"Zeitprogramm: Normal / Zirku: {zirku_text}"
        return f"Zeitprogramm / Zirku: {zirku_text}"
    else:  # HAND
        return f"HAND Spar / Zeitprogramm: Zirku: {zirku_text}"


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

    w0 = get_modbus_dec(words, 0, 1, signed=False) or 0
    w1 = mode or 0
    r["status_bits_word0"] = w0
    r["Statusmeldung"] = extract_fwe_status_meldung(w0, w1)
    prg_map = {0: "Aus", 1: "Spar", 2: "Normal"}
    r["Programmzustand"] = prg_map.get((w0 >> 8) & 0x03, "Normal")
    r["Zirkulation_Aktiv"] = bool(w0 & 0x0020)
    r["Zirkulation_Freigabe"] = bool(w0 & 0x0400)
    r["Tauscherabkuehlung"] = bool(((w0 >> 13) & 0x07) == 4)
    r["Fehler"] = bool(w0 & 0x001F)
    r["Fehler_EEPROM"] = bool(w0 & 0x0001)
    r["Fehler_Parameter"] = bool(w0 & 0x0002)
    r["Fehler_Programm"] = bool(w0 & 0x0004)
    r["Fehler_Sensoren"] = bool(w0 & 0x0008)
    r["Kein_Durchfluss_Zirku"] = bool(w0 & 0x0010)
    return r


def extract_sk_status_meldung(w0: int, leistung: float) -> str:
    """Exact reproduction of EB7000 firmware RefreshSKMessages (gui_sk.js)."""
    if w0 & 0x0001:
        return "Fehler: EEPROM"
    if w0 & 0x0002:
        return "Fehler: Parameter"
    if w0 & 0x0004:
        return "Fehler: Programm"
    if w0 & 0x0008:
        return "Fehler: Sensoren überprüfen"
    if w0 & 0x0010:
        return "Durchfluss überprüfen"

    if (w0 & 0x0080) == 0:
        return "Solar Nachtabschaltung"
    if w0 & 0x0200:
        return "Notausfunktion aktiv"

    reglerstatus = (w0 >> 10) & 0x0F
    if reglerstatus in (0, 1):
        return "Solar Pause"
    elif reglerstatus in (2, 3):
        return "Anlauf"
    elif reglerstatus == 4:
        return f"Solar Aktiv (highFlow: {leistung:.1f} kW)"
    elif reglerstatus == 5:
        return f"Solar Aktiv (lowFlow: {leistung:.1f} kW)"
    elif reglerstatus == 6:
        return f"Solar Aktiv ({leistung:.1f} kW)"
    elif reglerstatus in (7, 8):
        return f"Solar Aktiv (HK_Direkt: {leistung:.1f} kW)"
    elif reglerstatus == 9:
        return f"Solar Aktiv (Einspritzen: {leistung:.1f} kW)"
    return "Solar Bereit"


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
    leistung = (low + high * 65535) / 1000.0
    r["Leistung_kW"] = leistung

    w0 = get_modbus_dec(words, 0, 1, signed=False) or 0
    r["status_bits_word0"] = w0
    r["Statusmeldung"] = extract_sk_status_meldung(w0, leistung)
    regler_map = {
        0: "Pause", 1: "Pause",
        2: "Anlauf", 3: "Anlauf",
        4: "HighFlow", 5: "LowFlow",
        6: "Notkühlung",
        7: "HK Bedarf", 8: "HK Bedarf",
        9: "Einspritzen",
    }
    regler_idx = (w0 >> 10) & 0x0F
    r["Reglerstatus"] = regler_map.get(regler_idx, f"Status_{regler_idx}")
    r["Nachtabschaltung"] = bool((w0 & 0x0080) == 0)
    r["Notaus"] = bool(w0 & 0x0200)
    r["Fehler"] = bool(w0 & 0x001F)
    r["Fehler_EEPROM"] = bool(w0 & 0x0001)
    r["Fehler_Parameter"] = bool(w0 & 0x0002)
    r["Fehler_Programm"] = bool(w0 & 0x0004)
    r["Fehler_Sensoren"] = bool(w0 & 0x0008)
    r["Durchfluss_Pruefen"] = bool(w0 & 0x0010)
    return r


def extract_hk_settings(words: List[int]) -> Dict[str, Any]:
    """
    Installer-level HK settings not read by the original web UI (which only
    ever fetched the first 16 words of this block). Offsets confirmed
    2026-09-02 by cross-referencing a raw Modbus dump against photos of the
    EB7000's physical control panel (FB/Heizkoerper/Fancoil circuits).
    """
    if len(words) < 60:
        return {}
    r: Dict[str, Any] = {}
    v14 = get_modbus_dec(words, 14, 1)
    r["Heizkurve_Steilheit"] = (v14 / 10.0) if v14 is not None else None
    v = get_modbus_dec(words, 15, 1)
    r["Parallelverschiebung"] = (v / 10.0) if v is not None else None
    v18 = get_modbus_dec(words, 18, 1)
    r["Vorlauf_Max"] = (v18 / 10.0) if v18 is not None and v18 not in (3150, -3150, 31500, -31500) else None
    v19 = get_modbus_dec(words, 19, 1)
    r["Vorlauf_Min"] = (v19 / 10.0) if v19 is not None and v19 not in (3150, -3150, 31500, -31500) else None
    v = get_modbus_dec(words, 20, 1)
    r["Absenkung"] = (v / 10.0) if v is not None else None
    v = get_modbus_dec(words, 21, 1)
    r["Schnellaufheizung"] = (v / 10.0) if v is not None else None
    v25 = get_modbus_dec(words, 25, 1)
    r["Norm_Aussentemperatur"] = float(v25) if v25 is not None else None
    v = get_modbus_dec(words, 59, 1)
    r["AusAussentemperatur"] = (v / 10.0) if v is not None else None
    return r


# Last good installer settings per object key. The 64-word config block read
# (extract_hk_settings) intermittently fails (~9 % of polls on hk3, an EB1000
# extension module); without this the keys vanish from the state JSON and every
# Home Assistant sensor templated on them logs a "no attribute" warning and
# flickers to unknown. Keep serving the last good values instead.
_LAST_HK_SETTINGS: Dict[str, Dict[str, Any]] = {}


def apply_hk_settings(result_obj: Dict[str, Any], key: str, cfg_words: Optional[List[int]]) -> None:
    """Merge installer settings into ``result_obj``, falling back to the last good read."""
    fresh = extract_hk_settings(cfg_words) if cfg_words else {}
    if fresh:
        _LAST_HK_SETTINGS[key] = fresh
    result_obj.update(fresh or _LAST_HK_SETTINGS.get(key, {}))


def extract_fwe_settings(words: List[int]) -> Dict[str, Any]:
    """
    Installer-level FWE (Warmwasser) settings, same provenance as
    extract_hk_settings(). Offsets confirmed against WW-Daten and
    Zirkulation panel screens.
    """
    if len(words) < 53:
        return {}
    r: Dict[str, Any] = {}
    v = get_modbus_dec(words, 9, 1)
    r["WWNormalSollTemperatur"] = (v / 10.0) if v is not None else None
    v = get_modbus_dec(words, 10, 1)
    r["WWSparSollTemperatur"] = (v / 10.0) if v is not None else None
    v = get_modbus_dec(words, 51, 1, signed=False)
    r["ZirkulationPausenzeit"] = v
    v = get_modbus_dec(words, 52, 1)
    r["ZirkulationMaxLaufzeit"] = (v / 10.0) if v is not None else None
    return r


def extract_wq_status_meldung(w0: int) -> str:
    """Exact reproduction of EB7000 firmware refreshWQMessages (gui_wq.js)."""
    if w0 & 0x0001:
        return "Fehler: EEPROM"
    if w0 & 0x0002:
        return "Fehler: Parameter"
    if w0 & 0x0004:
        return "Fehler: Programm"
    if w0 & 0x0008:
        return "Fehler: Sensoren überprüfen"

    if (w0 & 0x0040) == 0:
        return "WQ extern blockiert"
    if w0 & 0x0400:
        return "Wärmeüberschuss"
    if w0 & 0x0020:
        return "Schornsteinfeger"
    if ((w0 >> 8) & 0x03) == 0:
        return "WQ gesperrt (Zeitprogramm)"
    if w0 & 0x0010:
        return "WQ aktiv"
    if (w0 & 0x0010) == 0 and ((w0 >> 12) & 0x07) == 3:
        return "WQ aktiv"
    return "Bereit"


def extract_wq_values(words: List[int]) -> Dict[str, Any]:
    if len(words) < 3:
        return {"error": "insufficient data"}
    r: Dict[str, Any] = {}
    for i, key in enumerate(["Betriebstemperatur", "Rücklauftemperatur"]):
        v = get_modbus_dec(words, 1 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None

    w0 = get_modbus_dec(words, 0, 1, signed=False) or 0
    r["status_bits_word0"] = w0
    r["Statusmeldung"] = extract_wq_status_meldung(w0)
    r["HW_Freigabe"] = bool(w0 & 0x0040)
    r["Angefordert"] = bool(w0 & 0x0010)
    r["Waermeueberschuss"] = bool(w0 & 0x0400)
    r["Schornsteinfeger"] = bool(w0 & 0x0020)
    r["Zeitprogramm_Gesperrt"] = bool(((w0 >> 8) & 0x03) == 0)
    r["Fehler"] = bool(w0 & 0x000F)
    r["Fehler_EEPROM"] = bool(w0 & 0x0001)
    r["Fehler_Parameter"] = bool(w0 & 0x0002)
    r["Fehler_Programm"] = bool(w0 & 0x0004)
    r["Fehler_Sensoren"] = bool(w0 & 0x0008)
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
    mode = get_modbus_dec(words, 1, 1, signed=False)
    r["mode"] = mode
    r["mode_name"] = WPSIEM_MODE_NAMES.get(mode, f"unknown({mode})") if mode is not None else None
    for i, key in enumerate(["Vorlauftemperatur", "Rücklauftemperatur", "Quellenaustritt", "Quelleneintritt", "KühlspeicherUnten"]):
        v = get_modbus_dec(words, 5 + i, 1)
        r[key] = (v / 10.0) if v is not None and v not in (3150, -3150, 31500, -31500) else None
    return r


def extract_wp4000_values(words: List[int]) -> Dict[str, Any]:
    r: Dict[str, Any] = {}
    if len(words) > 2:
        mode = get_modbus_dec(words, 2, 1, signed=False)
        r["mode"] = mode
        r["mode_name"] = WP4000_MODE_NAMES.get(mode, f"unknown({mode})") if mode is not None else None
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
            # hk1/hk2/fwe blocks are confirmed readable up to 64 words (see
            # extract_hk_settings/extract_fwe_settings); other object types
            # keep the original 16-word read since their extents beyond that
            # aren't mapped to named settings yet.
            cfg_count = 64 if key in ("hk1", "hk2", "fwe") else 16
            cfg_words = read_actual_values_fc03(unit_int, start, cfg_count, host, port)
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
            if key in ("hk1", "hk2"):
                apply_hk_settings(result[key], key, cfg_words)
            elif key == "fwe" and cfg_words:
                result[key].update(extract_fwe_settings(cfg_words))

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
            cfg_words_eb = read_actual_values_fc03(unit_eb, 0x2000, 64, host, port)
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
                apply_hk_settings(result[hk_key], hk_key, cfg_words_eb)

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
    "set_fwe_mode",
    "set_fwe_temp",
    "set_wpsiem_mode",
    "set_wp4000_mode",
    "HK_MODE_NAMES",
    "FWE_MODE_NAMES",
    "WPSIEM_MODE_NAMES",
    "WP4000_MODE_NAMES",
    "build_hk_urlaub_fc4c_hex",
    "extract_wpint_values",
    "extract_wpsiem_values",
    "extract_wp4000_values",
    "extract_hk_settings",
    "extract_fwe_settings",
    "extract_hk_values",
    "extract_fwe_values",
    "extract_sp_values",
    "extract_sk_values",
    "extract_wq_values",
    "extract_hk_status_meldung",
    "extract_fwe_status_meldung",
    "extract_sp_status_meldung",
    "extract_sk_status_meldung",
    "extract_wq_status_meldung",
]


