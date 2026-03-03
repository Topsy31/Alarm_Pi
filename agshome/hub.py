"""
hub.py — AGSHome alarm hub wrapper using TinyTuya.

Provides a high-level interface to the AGSHome security hub over the
local network (Tuya encrypted protocol). No cloud dependency at runtime.

Architecture (Pi-owned state machine):
    The hub is kept in HOME + MUTE permanently after initial setup.
    The Pi controls all alarm logic — the hub acts as a dumb peripheral
    that fires sensor events (DPS 116) and receives siren commands (DPS 104).

    ensure_home_muted()  — call at startup and after reconnect (one write max)
    siren_on()           — fire siren directly via DPS 104
    siren_off()          — silence siren via DPS 104
    set_night_light()    — toggle night light (also DPS 104)
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

import tinytuya

from .dps_map import (
    AlarmMode, VolumeLevel,
    DPS_ALARM_MODE, DPS_ALARM_TRIGGERED, DPS_SIREN,
    DPS_VOLUME, DPS_SENSOR_EVENT, DPS_NOTIFICATION,
    describe_dps, decode_utf16_base64,
)

logger = logging.getLogger(__name__)


class AGSHomeHub:
    """
    Interface to an AGSHome alarm hub via TinyTuya local protocol.

    Usage:
        hub = AGSHomeHub(device_id="...", ip_address="...", local_key="...")
        if hub.connect():
            hub.ensure_home_muted()   # one-time setup at startup
            hub.siren_on()            # fire siren directly
            hub.siren_off()           # silence siren
        hub.disconnect()
    """

    def __init__(
        self,
        device_id: str,
        ip_address: str,
        local_key: str,
        version: float = 3.4,
    ):
        self.device_id = device_id
        self.ip_address = ip_address
        self.local_key = local_key
        self.version = version
        self._device: Optional[tinytuya.Device] = None
        self._device_lock = threading.Lock()  # serialise all socket access
        self._listeners: list[Callable] = []
        self._last_status: dict = {}

        # Monitor state
        self._monitor_active = False
        self._monitor_mode = ""           # "night", "silent_night", "dog_door", "away"
        self._monitor_listeners: list[Callable] = []

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def connect(self) -> bool:
        """
        Connect to the hub on the local network.

        Tries the configured IP first. If that fails, runs UDP discovery
        to find the hub by device ID (handles DHCP address changes).

        Returns True on success.
        """
        if self._try_connect(self.ip_address):
            return True

        logger.info("Configured IP failed, scanning network for hub...")
        discovered_ip = self._discover_device()
        if discovered_ip and discovered_ip != self.ip_address:
            logger.info(f"Hub discovered at new IP: {discovered_ip}")
            if self._try_connect(discovered_ip):
                self.ip_address = discovered_ip
                return True

        logger.error("Hub connection failed (configured IP and discovery both failed)")
        return False

    def _try_connect(self, ip: str) -> bool:
        """Attempt connection to the hub at a specific IP."""
        try:
            device = tinytuya.Device(
                dev_id=self.device_id,
                address=ip,
                local_key=self.local_key,
                version=self.version,
            )
            result = device.status()

            if "Error" in result or "Err" in result:
                logger.warning(f"Connection to {ip} failed: {result}")
                return False

            self._device = device
            self._last_status = result.get("dps", {})
            logger.info(f"Hub connected at {ip} (v{self.version})")
            logger.info(f"Initial DPS: {self._last_status}")
            return True

        except Exception as e:
            logger.warning(f"Connection to {ip} failed: {e}")
            return False

    def _discover_device(self) -> Optional[str]:
        """Scan the local network for the hub by device ID via UDP broadcast."""
        try:
            logger.info("Running TinyTuya UDP discovery...")
            devices = tinytuya.deviceScan(verbose=False, maxretry=15)
            for ip, info in devices.items():
                if info.get("gwId") == self.device_id:
                    logger.info(f"Found hub: {ip} (version {info.get('version', '?')})")
                    return ip
            logger.warning(f"Device {self.device_id} not found on network")
            return None
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            return None

    def disconnect(self):
        """Close the connection to the hub."""
        if self._device:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
            logger.info("Hub disconnected.")

    def is_connected(self) -> bool:
        """Check if we have an active device handle."""
        return self._device is not None

    def reconnect(self) -> bool:
        """Gracefully disconnect and reconnect. Returns True on success."""
        logger.info("Hub: graceful reconnect starting...")
        self.disconnect()
        time.sleep(3)
        return self.connect()

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        """
        Query the hub's current DPS state.

        Returns a flat {dps_index: value} dict, or {"error": message} on failure.
        """
        if not self._device:
            return {"error": "Not connected"}
        try:
            with self._device_lock:
                result = self._device.status()
            if "Error" in result or "Err" in result:
                return {"error": str(result)}
            return result.get("dps", {})
        except Exception as e:
            return {"error": str(e)}

    def status_pretty(self) -> list[str]:
        """Return human-readable status lines."""
        dps = self.status()
        if "error" in dps:
            return [f"Error: {dps['error']}"]
        return [describe_dps(idx, val) for idx, val in sorted(dps.items())]

    def health_check(self) -> bool:
        """
        Perform a status() poll to verify the hub is still responsive.

        Returns True if healthy, False on 914 lockout or connection error.
        Sets self._device = None on 914 so the monitor loop can detect it.
        """
        if not self._device:
            return False
        try:
            with self._device_lock:
                result = self._device.status()
            if isinstance(result, dict) and result.get("Err") == "914":
                logger.warning("Health check: hub returned 914 — marking disconnected")
                self._device = None
                return False
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Low-level DPS write
    # ------------------------------------------------------------------ #

    def _set_dps(self, index: str, value: Any) -> bool:
        """Send a DPS value to the hub, reconnecting once if session has expired (914)."""
        with self._device_lock:
            if not self._device:
                logger.error("Not connected")
                return False
            try:
                result = self._device.set_value(index, value)
            except Exception as e:
                logger.error(f"Failed to set DPS {index}: {e}")
                return False

        # 914 = session key expired — reconnect outside the lock (network call)
        if isinstance(result, dict) and result.get("Err") == "914":
            logger.warning("Session expired (914) — reconnecting and retrying...")
            if not self.connect():
                logger.error(f"Reconnection failed, could not set DPS {index}")
                return False
            with self._device_lock:
                if not self._device:
                    return False
                try:
                    result = self._device.set_value(index, value)
                except Exception as e:
                    logger.error(f"Failed to set DPS {index} after reconnect: {e}")
                    return False

        logger.info(f"Set DPS {index} = {value} -> {result}")
        return True

    def set_dps_value(self, index: str, value: Any) -> bool:
        """Set an arbitrary DPS value (for testing/discovery)."""
        return self._set_dps(index, value)

    # ------------------------------------------------------------------ #
    # Pi-owned siren control (DPS 104)
    # ------------------------------------------------------------------ #

    def ensure_home_muted(self) -> bool:
        """
        Set hub to HOME + MUTE if not already in that state.

        Called at startup and after any reconnect. This is the only place
        DPS 101 (mode) is written — one-time setup, never during normal operation.
        Returns True if already correct or successfully set.
        """
        status = self.status()
        if "error" in status:
            logger.warning(f"ensure_home_muted: status error — {status['error']}")
            return False
        mode = status.get(DPS_ALARM_MODE)
        volume = status.get(DPS_VOLUME)
        changed = False
        if mode != AlarmMode.HOME.value:
            self._set_dps(DPS_ALARM_MODE, AlarmMode.HOME.value)
            changed = True
        if volume != VolumeLevel.MUTE.value:
            self._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
            changed = True
        if changed:
            logger.info("Hub: set to HOME+MUTE (one-time setup)")
        else:
            logger.info("Hub: already HOME+MUTE — no write needed")
        return True

    def siren_on(self) -> None:
        """Fire the siren directly via DPS 104."""
        self._set_dps(DPS_SIREN, True)
        logger.info("Siren: ON")

    def siren_off(self) -> None:
        """Silence the siren directly via DPS 104."""
        self._set_dps(DPS_SIREN, False)
        logger.info("Siren: OFF")

    def set_night_light(self, on: bool) -> bool:
        """Turn the night light on or off (DPS 104)."""
        return self._set_dps(DPS_SIREN, on)

    # ------------------------------------------------------------------ #
    # Monitor mode
    # ------------------------------------------------------------------ #

    @property
    def monitor_active(self) -> bool:
        return self._monitor_active

    def add_monitor_listener(self, callback: Callable[[str, str], None]):
        """
        Register a callback for monitor events.
        Callback signature: callback(event_type, message)
        """
        self._monitor_listeners.append(callback)

    def _notify_monitor(self, event_type: str, message: str):
        """Notify all monitor listeners."""
        for listener in self._monitor_listeners:
            try:
                listener(event_type, message)
            except Exception as e:
                logger.error(f"Monitor listener error: {e}")

    def start_monitor(self, mode: str):
        """
        Enter monitor mode for the given mode string.

        mode must be one of: "night", "silent_night", "dog_door", "away"

        The hub is already in HOME+MUTE (set at startup). This method only
        records the current Pi mode so listeners can see it — it does not
        arm/disarm the hub.
        """
        if self._monitor_active:
            logger.warning("Monitor already active — stopping first")
            self.stop_monitor()

        self._monitor_active = True
        self._monitor_mode = mode
        logger.info(f"Monitor started (mode: {mode})")
        self._notify_monitor("info", f"Monitor started ({mode})")

    def stop_monitor(self):
        """Exit monitor mode."""
        self._monitor_active = False
        self._monitor_mode = ""
        logger.info("Monitor stopped")
        self._notify_monitor("info", "Monitor stopped")

    def monitor_check_async(self) -> list[dict]:
        """
        Receive one async push packet from the hub (non-blocking, 0.1s timeout).

        Returns a list of event dicts: [{"type": str, "message": str, "dps": dict}]

        Event types:
            "sensor"       — DPS 116: sensor name
            "triggered"    — DPS 103: alarm triggered bool
            "mode"         — DPS 101: hub mode changed (remote control actions)
            "siren"        — DPS 104: siren/night light state
            "notification" — DPS 121: hub notification string
        """
        events = []
        if not self._device:
            return events

        try:
            with self._device_lock:
                self._device.set_socketTimeout(0.1)
                data = self._device.receive()

            if not data or not isinstance(data, dict):
                return events

            dps = data.get("dps", {})
            if not dps and "data" in data:
                dps = data["data"].get("dps", {})
            if not dps:
                return events

            logger.debug(f"Async DPS received: {dps}")

            if DPS_SENSOR_EVENT in dps:
                sensor_name = decode_utf16_base64(dps[DPS_SENSOR_EVENT])
                events.append({"type": "sensor", "message": sensor_name, "dps": dps})
                logger.info(f"Monitor: sensor event — {sensor_name}")

            if DPS_NOTIFICATION in dps:
                notification = decode_utf16_base64(dps[DPS_NOTIFICATION])
                events.append({"type": "notification", "message": notification, "dps": dps})

            if DPS_ALARM_TRIGGERED in dps:
                triggered = dps[DPS_ALARM_TRIGGERED]
                events.append({"type": "triggered", "message": str(triggered), "dps": dps})
                logger.info(f"Monitor: DPS 103 triggered = {triggered}")

            if DPS_ALARM_MODE in dps:
                events.append({"type": "mode", "message": dps[DPS_ALARM_MODE], "dps": dps})
                logger.info(f"Monitor: mode change — {dps[DPS_ALARM_MODE]}")

            if DPS_SIREN in dps:
                events.append({"type": "siren", "message": str(dps[DPS_SIREN]), "dps": dps})

            self._last_status.update(dps)

        except Exception:
            pass

        return events

    # ------------------------------------------------------------------ #
    # Polling (legacy — used by dashboard)
    # ------------------------------------------------------------------ #

    def poll_once(self) -> dict:
        """Poll the hub once and fire listeners for any changes."""
        current = self.status()
        if "error" in current:
            return current
        for idx, val in current.items():
            old = self._last_status.get(idx)
            if old is not None and old != val:
                for listener in self._listeners:
                    try:
                        listener(idx, val, old)
                    except Exception as e:
                        logger.error(f"Listener error: {e}")
        self._last_status = dict(current)
        return current

    def add_listener(self, callback: Callable[[str, Any, Any], None]):
        """Register a callback for DPS changes: callback(dps_index, new_value, old_value)"""
        self._listeners.append(callback)

    def poll_loop(self, interval: float = 5.0):
        """Blocking poll loop — queries hub at regular intervals."""
        logger.info(f"Polling hub every {interval}s (Ctrl+C to stop)...")
        while True:
            self.poll_once()
            time.sleep(interval)

    # ------------------------------------------------------------------ #
    # Utilities
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        status = "connected" if self.is_connected() else "disconnected"
        return f"AGSHomeHub(id={self.device_id}, ip={self.ip_address}, {status})"
