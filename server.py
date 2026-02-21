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

from flask import Flask, jsonify, render_template, Response

from agshome.hub import AGSHomeHub
from agshome.dps_map import (
    AlarmMode, VolumeLevel,
    DPS_SIREN, DPS_VOLUME,
    decode_utf16_base64,
)
from camera import OKamCamera, CameraConfig

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

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
        self.camera: Optional[OKamCamera] = None
        self.camera_connected = False
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


def create_camera(config: dict) -> Optional[OKamCamera]:
    """Create camera instance from config."""
    cc = config.get("camera", {})
    if not cc.get("ip_address") or cc["ip_address"].startswith("YOUR"):
        return None
    cam_config = CameraConfig(
        name=cc.get("name", "O-KAM Camera"),
        ip_address=cc["ip_address"],
        rtsp_port=cc.get("rtsp_port", 10555),
        stream_path=cc.get("stream_path", "TCP/av0_0"),
        sub_stream_path=cc.get("sub_stream_path", "TCP/av0_1"),
        use_sub_stream=cc.get("use_sub_stream", False),
        username=cc.get("username", ""),
        password=cc.get("password", ""),
    )
    return OKamCamera(cam_config)


def connect_camera():
    """Connect to the camera (called once at startup)."""
    if not CV2_AVAILABLE:
        logger.warning("OpenCV not available — camera disabled")
        return
    config = load_config()
    camera = create_camera(config)
    if not camera:
        logger.info("No camera configured (check config.json)")
        return

    logger.info(f"Connecting to camera at {camera.config.ip_address}...")
    if camera.connect():
        camera.start_stream(display=False, fps_limit=15.0)
        state.camera = camera
        state.camera_connected = True
        logger.info(f"Camera connected at {camera.config.ip_address}")
    else:
        logger.error("Camera connection failed")


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


@app.route("/desktop")
def desktop():
    return render_template("desktop.html")


@app.route("/api/status")
def api_status():
    with state.lock:
        return jsonify({
            "mode": state.mode,
            "night_light": state.night_light,
            "hub_connected": state.hub_connected,
            "camera_connected": state.camera_connected,
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


@app.route("/api/camera/stream")
def api_camera_stream():
    """MJPEG stream from the camera for the desktop view."""
    if not state.camera_connected or not state.camera:
        return "Camera not connected", 503

    def generate():
        while True:
            frame = state.camera.get_latest_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            ret, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ret:
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + jpeg.tobytes()
                + b"\r\n"
            )
            time.sleep(0.066)  # ~15 fps

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/api/camera/snapshot")
def api_camera_snapshot():
    """Single JPEG frame from the camera."""
    if not state.camera_connected or not state.camera:
        return "Camera not connected", 503
    frame = state.camera.get_latest_frame()
    if frame is None:
        return "No frame available", 503
    ret, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ret:
        return "Encoding failed", 500
    return Response(jpeg.tobytes(), mimetype="image/jpeg")


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
    connect_camera()
    start_monitor_thread()
    local_ip = get_local_ip()
    print(f"\n  Phone:   http://agshome.local:5000")
    print(f"  Desktop: http://agshome.local:5000/desktop")
    print(f"  (or by IP: http://{local_ip}:5000)\n")
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        stop_monitor_thread()
        if state.camera:
            state.camera.disconnect()
        if state.hub:
            if state.hub.monitor_active:
                state.hub.stop_monitor()
            state.hub.disconnect()
