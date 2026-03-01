"""
hub.py — AGSHome alarm hub wrapper using TinyTuya.

Provides a high-level interface to the AGSHome security hub over the
local network (Tuya encrypted protocol). No cloud dependency at runtime.

Mode functions:
    disarm()        — disarm the hub
    arm_away()      — full alarm, hub owns siren, no auto-rearm
    arm_loud()      — HOME mode, HIGH volume, beep on arm (Night)
    arm_silent()    — HOME mode, MUTE volume, no beep (Day/Silent Night/Dog Door)

Rearm sequences (called by monitor loop on trigger):
    rearm_night()   — wait 30s, cut siren, silent disarm/rearm, restore HIGH
    rearm_silent()  — immediate silent disarm/rearm, volume stays MUTE
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

NIGHT_SIREN_DURATION = 30  # seconds siren runs before auto-rearm in Night mode


class AGSHomeHub:
    """
    Interface to an AGSHome alarm hub via TinyTuya local protocol.

    Usage:
        hub = AGSHomeHub(device_id="...", ip_address="...", local_key="...")
        if hub.connect():
            hub.arm_loud()      # Night mode
            hub.arm_silent()    # Silent Night / Day / Dog Door
            hub.disarm()
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
        self._monitor_mode = ""           # "night", "day", "silent_night", "dog_door"
        self._monitor_rearming = False
        self._abort_rearm = False         # set True by external disarm to cancel rearm wait
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

    def set_night_light(self, on: bool) -> bool:
        """Turn the night light on or off (DPS 104)."""
        return self._set_dps(DPS_SIREN, on)

    # ------------------------------------------------------------------ #
    # Named mode functions
    # ------------------------------------------------------------------ #

    def disarm(self) -> bool:
        """Disarm the hub (DPS 101 = DISARMED)."""
        ok = self._set_dps(DPS_ALARM_MODE, AlarmMode.DISARMED.value)
        if ok:
            logger.info("Hub: disarmed")
        return ok

    def arm_away(self) -> bool:
        """
        AWAY mode: full alarm, hub owns siren, no auto-rearm.

        Sets volume HIGH then arms to AWAY. The hub handles the siren
        independently — our code does not interfere.
        """
        self._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
        time.sleep(0.5)
        ok = self._set_dps(DPS_ALARM_MODE, AlarmMode.AWAY.value)
        if ok:
            logger.info("Hub: armed AWAY (loud)")
        return ok

    def arm_loud(self) -> bool:
        """
        HOME mode, HIGH volume — used by Night mode.

        Hub beeps on arm. Siren sounds on trigger until rearm cuts it.
        """
        self._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
        time.sleep(0.5)
        ok = self._set_dps(DPS_ALARM_MODE, AlarmMode.HOME.value)
        if ok:
            logger.info("Hub: armed HOME loud (night)")
        return ok

    def arm_silent(self) -> bool:
        """
        HOME mode, MUTE volume — used by Day, Silent Night, Dog Door, and all rearms.

        No arm beep. No siren on trigger (volume muted throughout).
        Note: hub suppresses DPS 116 (sensor name) when muted — sensor name
        will not be available in ntfy or the app card.

        Note: the hub produces a short arm beep regardless of volume setting.
        This is driven by a hardware piezo in the DP-W2.1 firmware and cannot
        be suppressed via any DPS command. DPS 107 (volume) controls the siren
        and sensor alerts only — not the arm confirmation beep.
        """
        self._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
        ok = self._set_dps(DPS_ALARM_MODE, AlarmMode.HOME.value)
        if ok:
            logger.info("Hub: armed HOME silent (day/silent_night/dog_door)")
        return ok

    # ------------------------------------------------------------------ #
    # Rearm sequences
    # ------------------------------------------------------------------ #

    def rearm_night(self) -> bool:
        """
        Night rearm: wait NIGHT_SIREN_DURATION seconds (siren runs), then
        cut siren, silent disarm/rearm, restore HIGH volume.

        Watches _abort_rearm every second — if set (remote or app disarmed),
        exits immediately without rearming.

        Returns True if rearmed, False if aborted.
        """
        logger.info(f"Night rearm: siren running, waiting {NIGHT_SIREN_DURATION}s...")
        for _ in range(NIGHT_SIREN_DURATION):
            if self._abort_rearm:
                logger.info("Night rearm: aborted (external disarm detected)")
                self._abort_rearm = False
                return False
            time.sleep(1)

        logger.info("Night rearm: cutting siren, silent disarm/rearm...")
        self._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
        self._set_dps(DPS_ALARM_MODE, AlarmMode.DISARMED.value)
        time.sleep(1.0)
        self._set_dps(DPS_ALARM_MODE, AlarmMode.HOME.value)
        time.sleep(0.5)
        self._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
        logger.info("Night rearm: complete")
        return True

    def rearm_silent(self) -> bool:
        """
        Silent rearm: immediate disarm/rearm, volume stays MUTE throughout.

        Used by Day, Silent Night, Dog Door.
        Returns True always (no abort logic needed — no siren to wait out).
        """
        logger.info("Silent rearm: disarm/rearm...")
        self._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
        self._set_dps(DPS_ALARM_MODE, AlarmMode.DISARMED.value)
        time.sleep(1.0)
        self._set_dps(DPS_ALARM_MODE, AlarmMode.HOME.value)
        logger.info("Silent rearm: complete")
        return True

    def abort_rearm(self):
        """Signal the rearm sequence to abort (called when external disarm detected)."""
        self._abort_rearm = True
        logger.info("Hub: rearm abort signalled")

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
        event_type: "sensor", "rearm", "silence", "info", "aborted"
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

        mode must be one of: "night", "day", "silent_night", "dog_door"

        The hub must already be armed (arm_loud or arm_silent called before this).
        This method only sets the monitor state — it does not arm the hub.
        """
        if self._monitor_active:
            logger.warning("Monitor already active — stopping first")
            self.stop_monitor()

        self._monitor_active = True
        self._monitor_mode = mode
        self._monitor_rearming = False
        self._abort_rearm = False
        logger.info(f"Monitor started (mode: {mode})")
        self._notify_monitor("info", f"Monitor started ({mode})")

    def stop_monitor(self):
        """Exit monitor mode. Does not disarm the hub — caller is responsible."""
        self._monitor_active = False
        self._monitor_rearming = False
        self._abort_rearm = False
        self._monitor_mode = ""
        logger.info("Monitor stopped")
        self._notify_monitor("info", "Monitor stopped")

    def run_rearm_sequence(self, mode: str):
        """
        Background thread entry point: run the appropriate rearm sequence for mode.

        Called by the server monitor loop when a trigger is detected.
        """
        self._monitor_rearming = True
        try:
            if mode == "night":
                rearmed = self.rearm_night()
                if rearmed:
                    self._notify_monitor("rearm", "Re-armed (night)")
                else:
                    self._notify_monitor("aborted", "Rearm aborted — external disarm")
            else:
                # day, silent_night, dog_door — all use silent rearm
                self.rearm_silent()
                self._notify_monitor("rearm", f"Re-armed ({mode})")
        except Exception as e:
            logger.error(f"Rearm sequence error: {e}")
        finally:
            self._monitor_rearming = False

    def monitor_check_async(self) -> list[dict]:
        """
        Receive one async push packet from the hub (non-blocking, 0.1s timeout).

        Returns a list of event dicts: [{"type": str, "message": str, "dps": dict}]

        Event types:
            "sensor"       — DPS 116: sensor name (only in loud modes)
            "triggered"    — DPS 103: alarm triggered bool
            "mode"         — DPS 101: hub mode changed (includes remote control actions)
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
