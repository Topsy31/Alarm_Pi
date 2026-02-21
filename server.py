"""
server.py -- Flask REST API for mobile alarm control.

Serves a mobile-optimised HTML page and provides API endpoints
for controlling the AGSHome alarm hub from a phone browser.

Usage:
    python server.py                 # Run standalone (for testing)
    python tray.py                   # Production: launches via system tray
"""

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template

from agshome.hub import AGSHomeHub
from agshome.dps_map import (
    AlarmMode, VolumeLevel,
    DPS_SIREN, DPS_VOLUME,
    decode_utf16_base64,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Quieten Flask's request logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
DEVICES_FILE = "devices.json"

app = Flask(__name__)


# ============================================================
# Application State
# ============================================================

class AppState:
    """Thread-safe application state."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "disarmed"
        self.night_light = False
        self.hub_connected = False
        self.last_sensor_name = ""
        self.last_sensor_time = ""
        self.alert_seq = 0  # incremented on each sensor trigger
        self.hub: Optional[AGSHomeHub] = None
        self._monitor_running = False
        self._monitor_thread: Optional[threading.Thread] = None


state = AppState()


# ============================================================
# Hub Setup (reuses pattern from dashboard.py)
# ============================================================

def load_config() -> dict:
    """Load the combined config file."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def create_hub(config: dict) -> Optional[AGSHomeHub]:
    """Create the alarm hub instance from config."""
    hc = config.get("hub", {})
    if not hc.get("device_id") or hc["device_id"].startswith("YOUR"):
        if os.path.exists(DEVICES_FILE):
            with open(DEVICES_FILE) as f:
                devices = json.load(f)
            if devices:
                d = devices[0]
                hc = {
                    "device_id": d["id"],
                    "ip_address": d.get("ip", ""),
                    "local_key": d["key"],
                    "protocol_version": float(d.get("version", 3.4)),
                }

    if not hc.get("device_id"):
        return None

    return AGSHomeHub(
        device_id=hc["device_id"],
        ip_address=hc["ip_address"],
        local_key=hc["local_key"],
        version=hc.get("protocol_version", 3.4),
    )


# ============================================================
# Background Monitor
# ============================================================

def _monitor_loop():
    """Background thread: poll hub for async events."""
    while state._monitor_running:
        if state.hub and state.hub_connected and state.hub._device:
            try:
                events = state.hub.monitor_check_async()
                for event in events:
                    if event["type"] == "sensor":
                        with state.lock:
                            state.last_sensor_name = event["message"]
                            state.last_sensor_time = datetime.now().strftime("%H:%M:%S")
                            state.alert_seq += 1
                        logger.info(f"Sensor: {event['message']}")
                    dps = event.get("dps", {})
                    if DPS_SIREN in dps:
                        with state.lock:
                            state.night_light = dps[DPS_SIREN]
            except Exception:
                pass
        time.sleep(0.3)


def start_monitor_thread():
    """Start the background monitor thread."""
    state._monitor_running = True
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()
    state._monitor_thread = t


def stop_monitor_thread():
    """Stop the background monitor thread."""
    state._monitor_running = False
    if state._monitor_thread:
        state._monitor_thread.join(timeout=2)


# ============================================================
# Hub Connection
# ============================================================

def connect_hub():
    """Connect to the hub (called once at startup)."""
    config = load_config()
    hub = create_hub(config)
    if not hub:
        logger.error("No hub configured (check config.json / devices.json)")
        return

    logger.info(f"Connecting to hub at {hub.ip_address}...")
    if hub.connect():
        state.hub = hub
        state.hub_connected = True
        status = hub.status()
        if "error" not in status:
            state.night_light = status.get(DPS_SIREN, False)
        logger.info(f"Hub connected at {hub.ip_address}")
    else:
        logger.error("Hub connection failed")


def get_local_ip() -> str:
    """Get the machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ============================================================
# Mode Helpers
# ============================================================

def _stop_current_mode():
    """Stop any active monitor mode and disarm."""
    if state.hub and state.hub.monitor_active:
        state.hub.stop_monitor()
    elif state.hub:
        state.hub.set_mode(AlarmMode.DISARMED)


# ============================================================
# API Routes
# ============================================================

@app.route("/")
def index():
    return render_template("mobile.html")


@app.route("/api/status")
def api_status():
    with state.lock:
        return jsonify({
            "mode": state.mode,
            "night_light": state.night_light,
            "hub_connected": state.hub_connected,
            "last_sensor_name": state.last_sensor_name,
            "last_sensor_time": state.last_sensor_time,
            "alert_seq": state.alert_seq,
        })


@app.route("/api/disarm", methods=["POST"])
def api_disarm():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    with state.lock:
        state.mode = "disarmed"
    return jsonify({"ok": True, "mode": "disarmed"})


@app.route("/api/away", methods=["POST"])
def api_away():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Away: volume HIGH, full alarm — siren sounds until disarmed
    state.hub.set_volume(VolumeLevel.HIGH)
    state.hub.set_mode(AlarmMode.AWAY)
    with state.lock:
        state.mode = "away"
    return jsonify({"ok": True, "mode": "away"})


@app.route("/api/day", methods=["POST"])
def api_day():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Day monitor: volume MUTE, siren silent, normal re-arm (hub beeps — wanted)
    state.hub.start_monitor(muted=True, silent_rearm=False)
    with state.lock:
        state.mode = "day"
    return jsonify({"ok": True, "mode": "day"})


@app.route("/api/night", methods=["POST"])
def api_night():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Night monitor: volume HIGH, siren sounds, silent re-arm (no beeps)
    state.hub.set_volume(VolumeLevel.HIGH)
    state.hub.start_monitor(muted=False, silent_rearm=True)
    with state.lock:
        state.mode = "night"
    return jsonify({"ok": True, "mode": "night"})


@app.route("/api/silent_night", methods=["POST"])
def api_silent_night():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Silent night: volume MUTE, siren silent, silent re-arm (no beeps)
    # Phone vibrates on trigger instead
    state.hub.start_monitor(muted=True, silent_rearm=True)
    with state.lock:
        state.mode = "silent_night"
    return jsonify({"ok": True, "mode": "silent_night"})


@app.route("/api/nightlight", methods=["POST"])
def api_nightlight():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    with state.lock:
        state.night_light = not state.night_light
        new_state = state.night_light
    state.hub.set_night_light(new_state)
    return jsonify({"ok": True, "night_light": new_state})


@app.route("/api/test_alert", methods=["POST"])
def api_test_alert():
    """Simulate a sensor trigger for testing phone alerts.
    Also sets mode to silent_night so the phone alert fires."""
    with state.lock:
        state.mode = "silent_night"
        state.last_sensor_name = "Test Sensor"
        state.last_sensor_time = datetime.now().strftime("%H:%M:%S")
        state.alert_seq += 1
    logger.info("Test alert triggered")
    return jsonify({"ok": True, "alert_seq": state.alert_seq})


# ============================================================
# Standalone Entry Point
# ============================================================

if __name__ == "__main__":
    connect_hub()
    start_monitor_thread()
    local_ip = get_local_ip()
    print(f"\n  Open on phone: http://agshome.local:5000")
    print(f"  (or by IP: http://{local_ip}:5000)\n")
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor_thread()
        if state.hub:
            if state.hub.monitor_active:
                state.hub.stop_monitor()
            state.hub.disconnect()
