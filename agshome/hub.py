"""
hub.py — AGSHome alarm hub wrapper using TinyTuya.

Provides a high-level interface to the AGSHome security hub over the
local network (Tuya encrypted protocol). No cloud dependency at runtime.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

import tinytuya

from .dps_map import (
    AlarmMode, VolumeLevel, DPS_ALARM_MODE, DPS_ALARM_TRIGGERED, DPS_SIREN,
    DPS_VOLUME, DPS_ZONE_1_ENABLED, DPS_ZONE_2_ENABLED,
    DPS_SENSOR_EVENT, DPS_NOTIFICATION,
    describe_dps, decode_utf16_base64,
)

logger = logging.getLogger(__name__)


class AGSHomeHub:
    """
    Interface to an AGSHome alarm hub via TinyTuya local protocol.

    Usage:
        hub = AGSHomeHub(device_id="...", ip_address="...", local_key="...")
        if hub.connect():
            print(hub.status())
            hub.set_mode(AlarmMode.HOME)
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
        self._listeners: list[Callable] = []
        self._last_status: dict = {}

    def connect(self) -> bool:
        """
        Connect to the hub on the local network.

        Tries the configured IP first. If that fails, runs UDP discovery
        to find the hub by device ID (handles DHCP address changes).

        Returns True on success.
        """
        if self._try_connect(self.ip_address):
            return True

        # Configured IP failed — try discovery
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
            device.set_socketPersistent(True)
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

    # --- Status ---

    def status(self) -> dict:
        """
        Query the hub's current DPS state.

        Returns a dict of {dps_index: value}, or {"error": message} on failure.
        """
        if not self._device:
            return {"error": "Not connected"}

        try:
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

    # --- Control ---

    def set_mode(self, mode: AlarmMode) -> bool:
        """Set the alarm mode (away, home, disarmed)."""
        return self._set_dps(DPS_ALARM_MODE, mode.value)

    def trigger_siren(self, on: bool = True) -> bool:
        """Turn the siren on or off (DPS 104)."""
        return self._set_dps(DPS_SIREN, on)

    def set_night_light(self, on: bool = True) -> bool:
        """Turn the night light on or off (DPS 104 — shared with siren)."""
        return self._set_dps(DPS_SIREN, on)

    def set_volume(self, level: VolumeLevel) -> bool:
        """Set the hub volume level."""
        return self._set_dps(DPS_VOLUME, level.value)

    def set_dps_value(self, index: str, value: Any) -> bool:
        """Set an arbitrary DPS value (for testing/discovery)."""
        return self._set_dps(index, value)

    def _set_dps(self, index: str, value: Any) -> bool:
        """Send a DPS value to the hub."""
        if not self._device:
            logger.error("Not connected")
            return False

        try:
            result = self._device.set_value(index, value)
            logger.info(f"Set DPS {index} = {value} -> {result}")
            return True
        except Exception as e:
            logger.error(f"Failed to set DPS {index}: {e}")
            return False

    # --- Listeners ---

    def add_listener(self, callback: Callable[[str, Any, Any], None]):
        """
        Register a callback for DPS changes.

        Callback signature: callback(dps_index, new_value, old_value)
        """
        self._listeners.append(callback)

    def _fire_listeners(self, index: str, new_value: Any, old_value: Any):
        """Notify all listeners of a DPS change."""
        for listener in self._listeners:
            try:
                listener(index, new_value, old_value)
            except Exception as e:
                logger.error(f"Listener error: {e}")

    # --- Polling ---

    def poll_once(self) -> dict:
        """
        Poll the hub once and fire listeners for any changes.

        Returns the current DPS dict.
        """
        current = self.status()
        if "error" in current:
            return current

        for idx, val in current.items():
            old = self._last_status.get(idx)
            if old is not None and old != val:
                self._fire_listeners(idx, val, old)

        self._last_status = dict(current)
        return current

    def poll_loop(self, interval: float = 5.0):
        """
        Blocking poll loop — queries hub at regular intervals.

        Runs until KeyboardInterrupt.
        """
        logger.info(f"Polling hub every {interval}s (Ctrl+C to stop)...")
        while True:
            self.poll_once()
            time.sleep(interval)

    # --- Monitor Mode ---

    def __init_monitor_state(self):
        """Initialise monitor mode state (called lazily)."""
        if not hasattr(self, "_monitor_active"):
            self._monitor_active = False
            self._monitor_muted = False
            self._monitor_silent_rearm = True
            self._monitor_saved_volume: Optional[str] = None
            self._monitor_rearming = False
            self._monitor_listeners: list[Callable] = []

    @property
    def monitor_active(self) -> bool:
        """Whether monitor mode is currently running."""
        self.__init_monitor_state()
        return self._monitor_active

    @property
    def monitor_muted(self) -> bool:
        """Whether monitor mode is running with muted volume."""
        self.__init_monitor_state()
        return self._monitor_muted

    def add_monitor_listener(self, callback: Callable[[str, str], None]):
        """
        Register a callback for monitor mode sensor events.

        Callback signature: callback(event_type, message)
        event_type is one of: "sensor", "rearm", "silence", "info"
        """
        self.__init_monitor_state()
        self._monitor_listeners.append(callback)

    def _notify_monitor(self, event_type: str, message: str):
        """Notify all monitor listeners."""
        self.__init_monitor_state()
        for listener in self._monitor_listeners:
            try:
                listener(event_type, message)
            except Exception as e:
                logger.error(f"Monitor listener error: {e}")

    def start_monitor(self, muted: bool = False, silent_rearm: bool = True):
        """
        Enter monitor mode.

        Sets the hub to HOME mode so sensors are active, then
        monitors all events. When a sensor triggers:
          1. Immediately silences the siren
          2. Logs the sensor event
          3. Re-arms to HOME mode

        Args:
            muted: If True, set volume to mute before arming.
                   Volume is restored when monitor mode exits.
            silent_rearm: If True (default), re-arm using direct DPS writes
                   (no beeps). If False, use normal disarm/re-arm cycle
                   (hub beeps on arm — useful for daytime awareness).

        Call stop_monitor() to exit.
        """
        self.__init_monitor_state()
        if self._monitor_active:
            logger.warning("Monitor mode already active")
            return

        self._monitor_active = True
        self._monitor_muted = muted
        self._monitor_silent_rearm = silent_rearm

        # If muted, save current volume and set to mute
        if muted:
            status = self.status()
            if "error" not in status:
                self._monitor_saved_volume = status.get(DPS_VOLUME)
            self.set_volume(VolumeLevel.MUTE)
            logger.info("Monitor: volume muted")
            self._notify_monitor("info", "Volume muted")

        # Arm to HOME so sensors are active
        self.set_mode(AlarmMode.HOME)
        mode_label = "Monitor mode (muted)" if muted else "Monitor mode"
        logger.info(f"{mode_label} started (hub set to HOME)")
        self._notify_monitor("info", f"{mode_label} started")

    def stop_monitor(self):
        """Exit monitor mode and disarm the hub."""
        self.__init_monitor_state()
        if not self._monitor_active:
            return

        self._monitor_active = False

        # Restore volume if it was muted
        if self._monitor_muted and self._monitor_saved_volume is not None:
            self._set_dps(DPS_VOLUME, self._monitor_saved_volume)
            logger.info(f"Monitor: volume restored to {self._monitor_saved_volume}")
            self._notify_monitor("info", "Volume restored")
            self._monitor_saved_volume = None
        self._monitor_muted = False

        self.set_mode(AlarmMode.DISARMED)
        logger.info("Monitor mode stopped (hub disarmed)")
        self._notify_monitor("info", "Monitor mode stopped")

    def monitor_check_async(self) -> list[dict]:
        """
        Check for async events from the hub and handle monitor logic.

        Returns a list of event dicts: [{"type": str, "message": str, "dps": dict}]
        Call this frequently (every 200-500ms) for responsive monitoring.
        """
        self.__init_monitor_state()
        events = []

        if not self._device:
            return events

        try:
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

            # Sensor event (DPS 116 — may or may not arrive via async)
            if DPS_SENSOR_EVENT in dps:
                sensor_name = decode_utf16_base64(dps[DPS_SENSOR_EVENT])
                events.append({"type": "sensor", "message": sensor_name, "dps": dps})
                logger.info(f"Monitor: sensor event — {sensor_name}")
                self._notify_monitor("sensor", sensor_name)

            # Notification (DPS 121 — often contains sensor name)
            if DPS_NOTIFICATION in dps:
                notification = decode_utf16_base64(dps[DPS_NOTIFICATION])
                events.append({"type": "notification", "message": notification, "dps": dps})

            # Alarm triggered (DPS 103) — primary trigger for monitor re-arm
            if DPS_ALARM_TRIGGERED in dps:
                triggered = dps[DPS_ALARM_TRIGGERED]
                events.append({"type": "triggered", "message": str(triggered), "dps": dps})

                # In monitor mode: silence siren and re-arm in background thread
                if triggered and self._monitor_active and not self._monitor_rearming:
                    threading.Thread(
                        target=self._monitor_rearm_sequence,
                        daemon=True,
                    ).start()

            # Mode change
            if DPS_ALARM_MODE in dps:
                mode_val = dps[DPS_ALARM_MODE]
                events.append({"type": "mode", "message": mode_val, "dps": dps})

        except Exception:
            pass

        return events

    def _monitor_rearm_sequence(self):
        """Background thread: silence siren, clear trigger, re-arm.

        Uses either silent re-arm (direct DPS writes, no beeps) or normal
        re-arm (disarm/re-arm cycle, hub beeps) depending on the
        silent_rearm flag set in start_monitor().
        """
        self._monitor_rearming = True
        try:
            # 1. Silence siren
            logger.info("Monitor: silencing siren...")
            self.trigger_siren(False)
            self._notify_monitor("silence", "Siren silenced")

            time.sleep(0.3)

            if self._monitor_silent_rearm:
                # SILENT re-arm: direct DPS writes (no mode change = no beep)
                logger.info("Monitor: clearing trigger (silent)...")
                self._set_dps(DPS_ALARM_TRIGGERED, False)
                time.sleep(0.3)

                if self._monitor_active:
                    logger.info("Monitor: re-enabling zones...")
                    self._set_dps(DPS_ZONE_1_ENABLED, True)
                    time.sleep(0.2)
                    self._set_dps(DPS_ZONE_2_ENABLED, True)
                    self._notify_monitor("rearm", "Re-armed (silent)")
                    logger.info("Monitor: re-arm complete (silent)")
            else:
                # NORMAL re-arm: disarm then re-arm (hub beeps — wanted)
                logger.info("Monitor: normal disarm/re-arm cycle...")
                self.set_mode(AlarmMode.DISARMED)
                time.sleep(1.0)

                if self._monitor_active:
                    self.set_mode(AlarmMode.HOME)
                    self._notify_monitor("rearm", "Re-armed")
                    logger.info("Monitor: re-arm complete (normal)")
        except Exception as e:
            logger.error(f"Monitor re-arm error: {e}")
        finally:
            self._monitor_rearming = False

    # --- Utilities ---

    def __repr__(self) -> str:
        status = "connected" if self.is_connected() else "disconnected"
        return f"AGSHomeHub(id={self.device_id}, ip={self.ip_address}, {status})"
