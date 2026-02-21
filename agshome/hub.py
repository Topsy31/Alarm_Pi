"""
hub.py — AGSHome alarm hub wrapper using TinyTuya.

Provides a high-level interface to the AGSHome security hub over the
local network (Tuya encrypted protocol). No cloud dependency at runtime.
"""

import logging
import time
from typing import Any, Callable, Optional

import tinytuya

from .dps_map import AlarmMode, DPS_ALARM_MODE, DPS_SIREN, describe_dps

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

        Returns True on success.
        """
        try:
            device = tinytuya.Device(
                dev_id=self.device_id,
                address=self.ip_address,
                local_key=self.local_key,
                version=self.version,
            )
            device.set_socketPersistent(True)
            result = device.status()

            if "Error" in result or "Err" in result:
                logger.error(f"Hub connection error: {result}")
                return False

            self._device = device
            self._last_status = result.get("dps", {})
            logger.info(f"Hub connected at {self.ip_address} (v{self.version})")
            logger.info(f"Initial DPS: {self._last_status}")
            return True

        except Exception as e:
            logger.error(f"Hub connection failed: {e}")
            return False

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
        """Turn the siren on or off."""
        return self._set_dps(DPS_SIREN, on)

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

    # --- Utilities ---

    def __repr__(self) -> str:
        status = "connected" if self.is_connected() else "disconnected"
        return f"AGSHomeHub(id={self.device_id}, ip={self.ip_address}, {status})"
