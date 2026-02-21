"""
dps_map.py — Data Point Schema mapping for AGSHome alarm hubs.

Tuya devices expose their state via numbered "data points" (DPS).
Mapping confirmed against a live AGSHome DP-W2.1 hub (protocol 3.4).
"""

from enum import Enum


class AlarmMode(str, Enum):
    """Alarm operating modes (DPS 101 values, confirmed from live device)."""
    AWAY = "1"
    DISARMED = "2"
    HOME = "3"


# --- DPS index constants (confirmed from live DP-W2.1 hub) ---

DPS_ALARM_MODE = "101"        # Alarm mode: "1"=away, "2"=disarmed, "3"=home
DPS_ALARM_TRIGGERED = "103"   # Alarm triggered (bool)
DPS_SIREN = "104"             # Siren on/off (bool)
DPS_ALARM_DURATION = "105"    # Alarm/siren duration (int, likely minutes)
DPS_VOLUME = "106"            # Volume level (str, e.g. "7")
DPS_ENTRY_DELAY = "107"       # Entry/exit delay in seconds (str, e.g. "25")
DPS_ZONE_1_ENABLED = "111"    # Zone 1 enabled (bool)
DPS_ZONE_2_ENABLED = "112"    # Zone 2 enabled (bool)
DPS_ZONE_1_SENSITIVITY = "113"  # Zone 1 sensitivity (int)
DPS_ZONE_2_SENSITIVITY = "114"  # Zone 2 sensitivity (int)
DPS_SENSOR_EVENT = "116"        # Sensor event (base64-encoded UTF-16 string, e.g. "Office Window Alarm")
DPS_NOTIFICATION = "121"        # Status notification (base64-encoded UTF-16 string, e.g. "Disarm")

# Map of all known DPS indices to human-readable names
DPS_NAMES = {
    DPS_ALARM_MODE: "Alarm Mode",
    DPS_ALARM_TRIGGERED: "Alarm Triggered",
    DPS_SIREN: "Siren",
    DPS_ALARM_DURATION: "Alarm Duration",
    DPS_VOLUME: "Volume",
    DPS_ENTRY_DELAY: "Entry/Exit Delay",
    DPS_ZONE_1_ENABLED: "Zone 1 Enabled",
    DPS_ZONE_2_ENABLED: "Zone 2 Enabled",
    DPS_ZONE_1_SENSITIVITY: "Zone 1 Sensitivity",
    DPS_ZONE_2_SENSITIVITY: "Zone 2 Sensitivity",
    DPS_SENSOR_EVENT: "Sensor Event",
    DPS_NOTIFICATION: "Notification",
}

# Mode display labels
MODE_LABELS = {
    AlarmMode.DISARMED: "DISARMED",
    AlarmMode.HOME: "HOME",
    AlarmMode.AWAY: "AWAY",
}


def decode_utf16_base64(value: str) -> str:
    """Decode a base64-encoded UTF-16 string from the hub."""
    import base64
    try:
        raw = base64.b64decode(value)
        return raw.decode("utf-16-be").strip("\x00")
    except Exception:
        return value


def describe_dps(index: str, value) -> str:
    """Return a human-readable description of a DPS value."""
    name = DPS_NAMES.get(str(index), f"DPS {index}")

    if str(index) == DPS_ALARM_MODE:
        try:
            mode = AlarmMode(str(value))
            return f"{name}: {MODE_LABELS[mode]}"
        except ValueError:
            pass

    if str(index) in (DPS_SENSOR_EVENT, DPS_NOTIFICATION):
        decoded = decode_utf16_base64(str(value))
        return f"{name}: {decoded}"

    if isinstance(value, bool):
        return f"{name}: {'ON' if value else 'OFF'}"

    return f"{name}: {value}"
