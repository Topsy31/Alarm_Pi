"""
server.py -- Flask REST API for mobile alarm control.

Serves a mobile-optimised HTML page and provides API endpoints
for controlling the AGSHome alarm hub from a phone browser.

Architecture (Pi-owned state machine):
    The hub stays in HOME + MUTE permanently after startup.
    The Pi owns all mode logic and fires/silences the siren directly
    via DPS 104. No DPS 101 (mode) writes occur during normal operation,
    eliminating the 914 session-rejection errors caused by frequent rearms.

Usage:
    python server.py                 # Run standalone (for testing)
    python pi_service.py             # Production: run via systemd on Pi
"""

import json
import logging
import os
import socket
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template, Response

from agshome.hub import AGSHomeHub
from agshome.dps_map import (
    AlarmMode, VolumeLevel,
    DPS_ALARM_TRIGGERED, DPS_ALARM_MODE, DPS_SIREN, DPS_VOLUME,
    DPS_SENSOR_EVENT, decode_utf16_base64,
)
from camera import OKamCamera, CameraConfig

import requests as http_requests

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

SIREN_DURATION = 30  # seconds the siren runs in Night mode before auto-silence


# ============================================================
# Application State
# ============================================================

class AppState:
    """Thread-safe application state."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "disarmed"            # disarmed/away/night/silent_night/dog_door
        self.night_light = False
        self.hub_connected = False
        self.last_sensor_name = ""
        self.last_sensor_time = ""
        self.alert_seq = 0                # incremented on each trigger (drives phone alert)
        self.hub: Optional[AGSHomeHub] = None
        self.camera: Optional[OKamCamera] = None
        self.camera_connected = False
        self._monitor_running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._reconnect_in_progress: bool = False
        self.hub_connect_time: Optional[datetime] = None
        # Pi-owned siren state
        self._siren_running: bool = False
        self._abort_siren: bool = False


state = AppState()


# ============================================================
# ntfy Push Notifications
# ============================================================

_ntfy_enabled = False
_ntfy_server = "https://ntfy.sh"
_ntfy_topic = ""
_ntfy_priority_alert = 5   # urgent
_ntfy_priority_status = 2  # low


def _load_ntfy_config():
    """Load ntfy settings from config.json."""
    global _ntfy_enabled, _ntfy_server, _ntfy_topic
    global _ntfy_priority_alert, _ntfy_priority_status
    config = load_config()
    nc = config.get("ntfy", {})
    _ntfy_enabled = nc.get("enabled", False)
    _ntfy_server = nc.get("server", "https://ntfy.sh").rstrip("/")
    _ntfy_topic = nc.get("topic", "")
    _ntfy_priority_alert = nc.get("priority_alert", 5)
    _ntfy_priority_status = nc.get("priority_status", 2)
    if _ntfy_enabled and _ntfy_topic:
        logger.info(f"ntfy enabled: {_ntfy_server}/{_ntfy_topic[:8]}...")
    elif _ntfy_enabled:
        logger.warning("ntfy enabled but no topic configured")
        _ntfy_enabled = False


def send_ntfy(message: str, title: str = "AGSHome",
              priority: int | None = None, tags: str = ""):
    """Send a push notification via ntfy. Non-blocking."""
    if not _ntfy_enabled or not _ntfy_topic:
        return
    if priority is None:
        priority = _ntfy_priority_status

    def _send():
        try:
            headers = {
                "Title": title,
                "Priority": str(priority),
            }
            if tags:
                headers["Tags"] = tags
            http_requests.post(
                f"{_ntfy_server}/{_ntfy_topic}",
                data=message.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"ntfy send failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


# ============================================================
# Hub Setup
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
# Pi-owned Siren Control
# ============================================================

def _stop_siren_if_running():
    """Signal the siren thread to stop (called by disarm)."""
    if state._siren_running:
        state._abort_siren = True


def _run_night_siren():
    """
    Run siren for SIREN_DURATION seconds then silence.

    Volume is already HIGH (set at arm time). To cut the siren we use
    silence_siren() which briefly disarms/re-arms the hub — the only
    reliable way to stop a triggered hub siren. Volume is then restored
    to HIGH so the next trigger will also sound (retriggerable).
    """
    state._siren_running = True
    state._abort_siren = False
    try:
        for _ in range(SIREN_DURATION):
            if state._abort_siren:
                logger.info("Night siren: aborted by disarm")
                return
            time.sleep(1)
        if state.hub:
            state.hub.silence_siren()
            state.hub._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
            logger.info("Night siren: silenced after %ds — volume HIGH, ready for retrigger", SIREN_DURATION)
    except Exception as e:
        logger.error(f"Night siren error: {e}")
    finally:
        state._siren_running = False
        state._abort_siren = False


def _run_away_siren():
    """
    Run siren indefinitely until _abort_siren is set (by disarm).

    Volume is already HIGH (set at arm time). Siren is cut by muting
    volume — disarm restores MUTE permanently.
    """
    state._siren_running = True
    state._abort_siren = False
    try:
        while not state._abort_siren:
            time.sleep(1)
        logger.info("Away siren: aborted by disarm")
        # Disarm will restore MUTE — nothing to do here
    except Exception as e:
        logger.error(f"Away siren error: {e}")
    finally:
        state._siren_running = False
        state._abort_siren = False


# ============================================================
# Background Monitor
# ============================================================

def _handle_trigger(current_mode: str, sensor_name: str = ""):
    """
    Called when a sensor trigger is detected (push or poll).

    Sends ntfy, updates app state, fires siren if appropriate.
    Guards against duplicate firing while siren is already running.
    """
    if not state.hub:
        return

    # Guard: siren already running — do not stack another thread
    if state._siren_running:
        logger.debug("Trigger detected but siren already running — ignoring duplicate")
        return

    now_str = datetime.now().strftime("%H:%M:%S")
    display_name = sensor_name if sensor_name else "Sensor triggered"

    with state.lock:
        state.alert_seq += 1
        state.last_sensor_time = now_str
        state.last_sensor_name = display_name

    ntfy_body = f"{display_name} ({current_mode})"
    send_ntfy(ntfy_body, title="Alarm Triggered",
              priority=_ntfy_priority_alert, tags="rotating_light,warning")

    # Disarmed: nothing more to do
    if current_mode == "disarmed":
        logger.info("disarmed trigger: ntfy sent, no siren")
        return

    # Silent modes: ntfy only — do not write any DPS.
    # DPS 104 writes rotate the session key (same as DPS 101), causing 914 errors.
    # The hub LED clears itself after ~1 min via its own timer — acceptable for silent mode.
    if current_mode in ("silent_night", "dog_door"):
        logger.info(f"{current_mode} trigger: ntfy sent")
        return

    if current_mode == "night":
        threading.Thread(target=_run_night_siren, daemon=True).start()
    elif current_mode == "away":
        threading.Thread(target=_run_away_siren, daemon=True).start()


def _reflect_remote_action(hub_mode_val: str):
    """
    Called when DPS 101 changes unexpectedly (remote control or external arm/disarm).

    In the Pi-owned model the hub is always expected to be in HOME.
    The only meaningful external action we act on is a remote DISARM.
    If hub reports non-HOME, restore HOME+MUTE.
    """
    if not state.hub:
        return

    if hub_mode_val == AlarmMode.DISARMED.value:
        with state.lock:
            current = state.mode
        if current == "disarmed":
            return
        # Remote or physical keypad disarmed — stop siren, update Pi state
        _stop_siren_if_running()
        if state.hub.monitor_active:
            state.hub.stop_monitor()
        with state.lock:
            state.mode = "disarmed"
        send_ntfy("Disarmed via remote", tags="unlock")
        logger.info("Remote: disarmed")

    elif hub_mode_val == AlarmMode.AWAY.value:
        # Hub should never be in AWAY in Pi-owned model — restore HOME+MUTE
        logger.warning("Remote: hub in AWAY — restoring HOME+MUTE")
        state.hub.ensure_home_muted()

    elif hub_mode_val == AlarmMode.HOME.value:
        # Expected state — nothing to do
        pass


def _monitor_loop():
    """
    Background thread: receives async hub push events every 0.3s.

    Uses receive() only — no status() calls here. status() sends a new
    request on the same socket that receive() is listening on, which
    corrupts the session. Polling for trigger detection is handled by
    the separate _poll_loop() thread which uses its own connection.
    """
    while state._monitor_running:
        if state.hub and state.hub_connected and state.hub._device:
            try:
                events = state.hub.monitor_check_async()
                for event in events:

                    if event["type"] == "sensor":
                        sensor_name = event["message"]
                        with state.lock:
                            current_mode = state.mode
                            state.last_sensor_name = sensor_name
                        logger.info(f"Sensor push: {sensor_name} (mode: {current_mode})")
                        if state.hub.monitor_active:
                            _handle_trigger(current_mode, sensor_name)

                    elif event["type"] == "triggered" and event["message"] == "True":
                        with state.lock:
                            current_mode = state.mode
                        logger.info(f"Trigger push: DPS 103=True (mode: {current_mode})")
                        if state.hub.monitor_active:
                            _handle_trigger(current_mode)

                    elif event["type"] == "mode":
                        _reflect_remote_action(event["message"])

                    elif event["type"] == "siren":
                        with state.lock:
                            state.night_light = event["message"] == "True"

            except Exception as e:
                logger.warning(f"Monitor loop error: {e}")

        time.sleep(0.3)


def _poll_loop():
    """
    Background thread: polls hub status() to catch triggers and detect
    remote mode changes.

    Poll frequency is adaptive:
    - silent_night / dog_door: every 10s (async push unreliable in muted modes)
    - all other modes: every 30s (async push is reliable; poll is a safety net)

    Uses state.hub.status() directly — shares the hub's main connection so
    the key is always current after any reconnect. A separate TinyTuya device
    instance caused 914 cascades because its key went stale after silence_siren()
    reconnected the main hub socket.
    """
    last_poll = 0.0
    last_poll_triggered = False

    while state._monitor_running:
        time.sleep(0.5)

        if not (state.hub and state.hub_connected):
            continue

        now = time.time()

        with state.lock:
            current_mode = state.mode
        poll_interval = 10.0 if current_mode in ("silent_night", "dog_door") else 30.0

        if now - last_poll < poll_interval:
            continue

        last_poll = now
        try:
            dps = state.hub.status()
            if "error" in dps:
                if "914" in str(dps.get("error", "")):
                    logger.warning("Poll: hub returned 914")
                    with state.lock:
                        state.hub_connected = False
                    _trigger_reconnect()
                continue

            # Restore HOME+MUTE if hub drifted (e.g. power cycle)
            polled_mode = dps.get(DPS_ALARM_MODE)
            if polled_mode is not None and polled_mode != AlarmMode.HOME.value:
                logger.warning(f"Poll: hub DPS 101 = {polled_mode!r} (expected HOME) — restoring")
                _reflect_remote_action(polled_mode)
                if polled_mode != AlarmMode.DISARMED.value:
                    state.hub.ensure_home_muted()

            # Trigger detection via DPS 103
            triggered = bool(dps.get(DPS_ALARM_TRIGGERED, False))
            if triggered and not last_poll_triggered:
                raw_name = dps.get(DPS_SENSOR_EVENT)
                sensor_name = decode_utf16_base64(raw_name) if raw_name else None
                logger.info(f"Trigger poll: DPS 103=True, sensor={sensor_name!r} (mode: {current_mode})")
                if state.hub.monitor_active:
                    _handle_trigger(current_mode, sensor_name)
            last_poll_triggered = triggered

        except Exception as e:
            logger.warning(f"Poll error: {e}")


def start_monitor_thread():
    """Start the monitor and poll background threads."""
    state._monitor_running = True
    threading.Thread(target=_monitor_loop, daemon=True).start()
    threading.Thread(target=_poll_loop, daemon=True).start()


def stop_monitor_thread():
    """Stop the background monitor threads."""
    state._monitor_running = False
    if state._monitor_thread:
        state._monitor_thread.join(timeout=2)


# ============================================================
# Hub Connection + Watchdog
# ============================================================

def connect_hub_with_retry(max_minutes: int = 5) -> bool:
    """Try to connect hub, retrying every 30s for up to max_minutes."""
    deadline = time.time() + (max_minutes * 60)
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        logger.info(f"Hub connect attempt {attempt}...")
        if state.hub and state.hub.connect():
            state.hub_connected = True
            state.hub_connect_time = datetime.now()
            # Set hub to HOME+MUTE — the one-time setup write
            state.hub.ensure_home_muted()
            status = state.hub.status()
            if "error" not in status:
                with state.lock:
                    state.night_light = status.get(DPS_SIREN, False)
            return True
        remaining = int((deadline - time.time()) / 60)
        logger.warning(f"Hub connect failed — retrying in 30s ({remaining}min remaining)...")
        time.sleep(30)
    send_ntfy(
        "Hub unreachable after 5 min — manual power cycle needed",
        title="AGSHome Alert",
        priority=4,
        tags="warning",
    )
    return False


def _trigger_reconnect():
    """Fire-and-forget reconnect after mid-session 914 detection."""
    if state._reconnect_in_progress:
        return
    state._reconnect_in_progress = True

    def _do_reconnect():
        logger.warning("Hub lost mid-session — attempting recovery...")
        send_ntfy(
            "Hub connection lost — attempting reconnect",
            title="AGSHome Alert",
            priority=3,
            tags="warning",
        )
        if state.hub:
            state.hub.disconnect()
        time.sleep(15)  # Hub needs time to release TCP session
        success = connect_hub_with_retry(max_minutes=5)
        if success:
            logger.info("Mid-session recovery: hub reconnected")
            send_ntfy("Hub reconnected", priority=1, tags="white_check_mark")
        state._reconnect_in_progress = False

    threading.Thread(target=_do_reconnect, daemon=True).start()


def start_reconnect_thread():
    """No-op: daily reconnect removed.

    Session key rotation is handled on-demand by silence_siren() (reconnects
    immediately after every DPS 101 write) and by _set_dps() 914 retry logic.
    A scheduled reconnect was actively harmful — it tore down working connections
    and failed to re-establish them when the key had rotated.
    """
    pass


def connect_hub():
    """Start hub connection in background — Flask starts immediately regardless."""
    _load_ntfy_config()
    config = load_config()
    hub = create_hub(config)
    if not hub:
        logger.error("No hub configured (check config.json / devices.json)")
        return

    state.hub = hub
    def _connect_bg():
        logger.info(f"Connecting to hub at {hub.ip_address}...")
        connect_hub_with_retry(max_minutes=5)
        if state.hub_connected:
            logger.info(f"Hub connected at {hub.ip_address}")
            start_monitor_thread()
            start_reconnect_thread()
        else:
            logger.error("Hub connection failed after retries — app still serving")

    threading.Thread(target=_connect_bg, daemon=True).start()


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
# API Routes
# ============================================================

@app.after_request
def add_no_cache(response):
    """Prevent aggressive mobile browser caching."""
    if "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
            "dog_door_active": state.mode == "dog_door",
        })


@app.route("/api/disarm", methods=["POST"])
def api_disarm():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_siren_if_running()
    if state.hub and state.hub.monitor_active:
        state.hub.stop_monitor()
    if state.hub:
        # Only use silence_siren() (DISARMED→HOME) if hub is actively triggered.
        # Unconditional calls cause the Tuya app to see a HOME arm event every disarm.
        hub_status = state.hub.status()
        if hub_status.get(DPS_ALARM_TRIGGERED):
            state.hub.silence_siren()
        else:
            # Not triggered — just ensure volume is muted, no DPS 101 write needed
            state.hub._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
    with state.lock:
        state.mode = "disarmed"
    send_ntfy("Alarm disarmed", tags="unlock")
    return jsonify({"ok": True, "mode": "disarmed"})


@app.route("/api/away", methods=["POST"])
def api_away():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_siren_if_running()
    # Set volume HIGH now — before any trigger fires
    if state.hub:
        state.hub._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
        state.hub.start_monitor(mode="away")
    with state.lock:
        state.mode = "away"
    send_ntfy("Alarm set to AWAY", tags="lock")
    return jsonify({"ok": True, "mode": "away"})


@app.route("/api/night", methods=["POST"])
def api_night():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_siren_if_running()
    # Set volume HIGH now — before any trigger fires
    if state.hub:
        state.hub._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
        state.hub.start_monitor(mode="night")
    with state.lock:
        state.mode = "night"
    send_ntfy("Night monitor active", tags="moon")
    return jsonify({"ok": True, "mode": "night"})


@app.route("/api/silent_night", methods=["POST"])
def api_silent_night():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_siren_if_running()
    # Ensure MUTE before entering silent mode
    if state.hub:
        state.hub._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
        state.hub.start_monitor(mode="silent_night")
    with state.lock:
        state.mode = "silent_night"
    send_ntfy("Silent night active", tags="zzz")
    return jsonify({"ok": True, "mode": "silent_night"})


@app.route("/api/suspend", methods=["POST"])
def api_suspend():
    """
    Dog Door: mute-style monitoring with ntfy only (no siren).

    Only available in Night mode. Switches Pi state to dog_door
    so triggers are handled silently. Hub stays in HOME+MUTE unchanged.
    """
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    with state.lock:
        current_mode = state.mode
        if current_mode != "night":
            return jsonify({"error": "Dog Door only available in Night mode"}), 400
        state.mode = "dog_door"

    if state.hub:
        state.hub._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
        state.hub._monitor_mode = "dog_door"

    logger.info("Dog door: active (volume muted, ntfy-only mode)")
    send_ntfy("Dog door — alarm muted", tags="dog")
    return jsonify({"ok": True, "mode": "dog_door"})


@app.route("/api/suspend/cancel", methods=["POST"])
def api_suspend_cancel():
    """
    Cancel Dog Door: restore Night mode and volume.
    """
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    with state.lock:
        if state.mode != "dog_door":
            return jsonify({"ok": True, "mode": state.mode})
        state.mode = "night"

    if state.hub:
        state.hub._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
        state.hub._monitor_mode = "night"

    logger.info("Dog door cancelled — Night restored (volume HIGH)")
    send_ntfy("Dog door cancelled — Night restored", tags="lock")
    return jsonify({"ok": True, "mode": "night"})


@app.route("/api/nightlight", methods=["POST"])
def api_nightlight():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    with state.lock:
        state.night_light = not state.night_light
        new_state = state.night_light
    if state.hub:
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
    """Simulate a sensor trigger for testing phone alerts."""
    with state.lock:
        state.mode = "silent_night"
        state.last_sensor_name = "Test Sensor"
        state.last_sensor_time = datetime.now().strftime("%H:%M:%S")
        state.alert_seq += 1
    logger.info("Test alert triggered")
    send_ntfy(
        "Test Sensor (silent_night)",
        title="Sensor Triggered",
        priority=_ntfy_priority_alert,
        tags="rotating_light,warning",
    )
    return jsonify({"ok": True, "alert_seq": state.alert_seq})


# ============================================================
# Service Page
# ============================================================

@app.route("/service")
def service_page():
    return render_template("service.html")


@app.route("/api/service/status")
def api_service_status():
    with state.lock:
        uptime = None
        if state.hub_connect_time:
            delta = datetime.now() - state.hub_connect_time
            uptime = str(delta).split(".")[0]
        return jsonify({
            "hub_connected": state.hub_connected,
            "hub_ip": state.hub.ip_address if state.hub else None,
            "hub_uptime": uptime,
            "monitor_running": state._monitor_running,
            "monitor_mode": state.hub._monitor_mode if state.hub else "",
            "siren_running": state._siren_running,
            "reconnect_in_progress": state._reconnect_in_progress,
            "ntfy_enabled": _ntfy_enabled,
        })


@app.route("/api/service/reconnect", methods=["POST"])
def api_service_reconnect():
    if state._reconnect_in_progress:
        return jsonify({"ok": False, "error": "Reconnect already in progress"})
    _trigger_reconnect()
    return jsonify({"ok": True, "message": "Reconnect started"})


@app.route("/api/service/restart", methods=["POST"])
def api_service_restart():
    try:
        subprocess.Popen(["sudo", "systemctl", "restart", "agshome"])
        return jsonify({"ok": True, "message": "Service restart initiated"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/service/test_ntfy", methods=["POST"])
def api_service_test_ntfy():
    send_ntfy(
        "Test notification from AGSHome service panel",
        title="AGSHome Test",
        priority=3,
        tags="white_check_mark",
    )
    return jsonify({"ok": True})


@app.route("/api/service/logs")
def api_service_logs():
    try:
        result = subprocess.run(
            ["sudo", "journalctl", "-u", "agshome", "-n", "30", "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        return jsonify({"ok": True, "lines": lines})
    except Exception as e:
        return jsonify({"ok": False, "lines": [], "error": str(e)})


# ============================================================
# Standalone Entry Point
# ============================================================

if __name__ == "__main__":
    connect_hub()       # non-blocking — starts background thread
    connect_camera()
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
