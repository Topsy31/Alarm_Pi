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
from datetime import datetime, timedelta
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


# ============================================================
# Application State
# ============================================================

class AppState:
    """Thread-safe application state."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "disarmed"            # disarmed/away/night/day/silent_night/dog_door
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
        self.dog_door_prior_mode: str = ""   # mode to restore when Dog Door cancelled
        self.reconnect_hour: int = 15
        self._reconnect_in_progress: bool = False
        self.hub_connect_time: Optional[datetime] = None
        self.last_health_check_time: str = ""


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

def _handle_trigger(current_mode: str, sensor_name: str = ""):
    """
    Called when a sensor trigger is detected (push or poll).

    Sends ntfy, updates app state, and starts the rearm sequence.
    Guards against duplicate firing if rearm is already running.
    """
    if not state.hub:
        return
    if state.hub._monitor_rearming:
        logger.debug("Trigger detected but rearm already in progress — ignoring duplicate")
        return

    now_str = datetime.now().strftime("%H:%M:%S")
    display_name = sensor_name if sensor_name else "Sensor triggered"

    with state.lock:
        state.alert_seq += 1
        state.last_sensor_time = now_str
        state.last_sensor_name = sensor_name if sensor_name else "Sensor triggered"

    ntfy_body = f"{display_name} ({current_mode})"
    send_ntfy(ntfy_body, title="Alarm Triggered",
              priority=_ntfy_priority_alert, tags="rotating_light,warning")

    # Silent modes: no rearm — hub stays in HOME/MUTE, no siren to cut,
    # DPS 103 resets on its own. Any DPS 101 write causes a piezo beep.
    if current_mode in ("dog_door", "silent_night"):
        logger.info(f"{current_mode} trigger: ntfy sent, no rearm")
        return

    if state.hub.monitor_active:
        threading.Thread(
            target=state.hub.run_rearm_sequence,
            args=(current_mode,),
            daemon=True,
        ).start()


def _reflect_remote_action(hub_mode_val: str):
    """
    Called when DPS 101 changes unexpectedly (remote control or external arm/disarm).

    Maps hub mode values to app state and handles rearm abort on external disarm.
    Ignores changes we initiated ourselves (when _monitor_rearming is True).
    """
    if not state.hub:
        return

    # Mode changes we triggered ourselves during rearm — ignore
    if state.hub._monitor_rearming:
        return

    if hub_mode_val == AlarmMode.DISARMED.value:
        # Remote or app disarmed — abort any pending rearm wait
        if state.hub._monitor_active:
            state.hub.abort_rearm()
            state.hub.stop_monitor()
        with state.lock:
            state.mode = "disarmed"
            state.dog_door_prior_mode = ""
        send_ntfy("Disarmed via remote", tags="unlock")
        logger.info("Remote: disarmed")

    elif hub_mode_val == AlarmMode.AWAY.value:
        # Remote armed to AWAY
        with state.lock:
            state.mode = "away"
        send_ntfy("Away armed via remote", tags="lock")
        logger.info("Remote: armed AWAY")

    elif hub_mode_val == AlarmMode.HOME.value:
        # Remote armed to HOME — treat as Night
        if state.hub.monitor_active:
            state.hub.stop_monitor()
        state.hub.start_monitor(mode="night")
        with state.lock:
            state.mode = "night"
        send_ntfy("Night armed via remote", tags="moon")
        logger.info("Remote: armed HOME → night")


def _monitor_loop():
    """
    Background thread: receives async hub push events every 0.3s.

    Uses receive() only — no status() calls here. status() sends a new
    request on the same socket that receive() is listening on, which
    corrupts the session. Polling for muted-mode triggers is handled by
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
    Background thread: polls hub status() every 3s to catch triggers that
    the hub suppresses in muted modes (DPS 116/103 not pushed when muted).

    Uses a SEPARATE TinyTuya device instance so it doesn't interfere with
    the receive() session in _monitor_loop().
    Also performs the 30s health check.
    """
    import tinytuya as _tinytuya
    poll_interval = 3.0
    health_check_interval = 30.0
    last_poll = 0.0
    last_health_check = 0.0
    last_poll_triggered = False
    last_polled_hub_mode = None
    poll_device = None

    while state._monitor_running:
        time.sleep(0.5)

        if not (state.hub and state.hub_connected):
            poll_device = None
            continue

        now = time.time()

        # --- Lazy-create a dedicated poll device ---
        if poll_device is None:
            try:
                poll_device = _tinytuya.Device(
                    dev_id=state.hub.device_id,
                    address=state.hub.ip_address,
                    local_key=state.hub.local_key,
                    version=state.hub.version,
                )
                poll_device.set_socketTimeout(3)
                logger.info("Poll device connected")
            except Exception as e:
                logger.warning(f"Poll device init failed: {e}")
                poll_device = None
                continue

        # --- Poll every 3s for DPS 103 ---
        if now - last_poll >= poll_interval:
            last_poll = now
            try:
                result = poll_device.status()
                if isinstance(result, dict) and "dps" in result:
                    dps = result["dps"]

                    # Check for remote mode changes (DPS 101)
                    polled_mode = dps.get(DPS_ALARM_MODE)
                    if polled_mode is not None and polled_mode != last_polled_hub_mode:
                        last_polled_hub_mode = polled_mode
                        _reflect_remote_action(polled_mode)
                    elif polled_mode is not None:
                        last_polled_hub_mode = polled_mode

                    triggered = bool(dps.get(DPS_ALARM_TRIGGERED, False))
                    if triggered and not last_poll_triggered:
                        with state.lock:
                            current_mode = state.mode
                        raw_name = dps.get(DPS_SENSOR_EVENT)
                        sensor_name = decode_utf16_base64(raw_name) if raw_name else None
                        logger.info(f"Trigger poll: DPS 103=True, sensor={sensor_name!r} (mode: {current_mode})")
                        if state.hub and state.hub.monitor_active:
                            _handle_trigger(current_mode, sensor_name)
                    last_poll_triggered = triggered
                elif isinstance(result, dict) and result.get("Err") == "914":
                    logger.warning("Poll: hub returned 914")
                    poll_device = None
            except Exception as e:
                logger.warning(f"Poll error: {e}")
                poll_device = None

        # --- Health check every 30s ---
        if now - last_health_check >= health_check_interval:
            last_health_check = now
            state.last_health_check_time = datetime.now().strftime("%H:%M:%S")
            if state.hub and not state.hub.health_check():
                with state.lock:
                    state.hub_connected = False
                poll_device = None
                _trigger_reconnect()


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
    """Try to connect hub, retrying every 30s for up to max_minutes. Sends ntfy if exhausted."""
    deadline = time.time() + (max_minutes * 60)
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        logger.info(f"Hub connect attempt {attempt}...")
        if state.hub and state.hub.connect():
            state.hub_connected = True
            state.hub_connect_time = datetime.now()
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
        time.sleep(3)
        success = connect_hub_with_retry(max_minutes=5)
        if success:
            logger.info("Mid-session recovery: hub reconnected")
            send_ntfy("Hub reconnected", priority=1, tags="white_check_mark")
        state._reconnect_in_progress = False

    threading.Thread(target=_do_reconnect, daemon=True).start()


def _daily_reconnect_loop():
    """Background thread: gracefully reconnect hub daily at state.reconnect_hour."""
    while True:
        now = datetime.now()
        target = now.replace(hour=state.reconnect_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info(f"Daily reconnect scheduled in {wait_secs/3600:.1f}h at {target.strftime('%H:%M')}")
        time.sleep(wait_secs)

        logger.info("Daily reconnect: starting graceful reconnect...")
        send_ntfy("Daily hub reconnect starting", priority=1, tags="arrows_counterclockwise")
        if state.hub:
            state.hub.disconnect()
        with state.lock:
            state.hub_connected = False
        time.sleep(3)
        success = connect_hub_with_retry(max_minutes=5)
        if success:
            logger.info("Daily reconnect: success")
            send_ntfy("Hub reconnected successfully", priority=1, tags="white_check_mark")


def start_reconnect_thread():
    """Start the daily reconnect watchdog thread."""
    t = threading.Thread(target=_daily_reconnect_loop, daemon=True)
    t.start()


def connect_hub():
    """Start hub connection in background — Flask starts immediately regardless."""
    _load_ntfy_config()
    config = load_config()
    hub = create_hub(config)
    if not hub:
        logger.error("No hub configured (check config.json / devices.json)")
        return

    state.hub = hub
    state.reconnect_hour = config.get("hub", {}).get("reconnect_hour", 15)

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
# Mode Helpers
# ============================================================

def _stop_current_mode():
    """
    Stop any active monitor mode and disarm the hub. Clears Dog Door state.

    Every mode activation calls this first to ensure a clean transition
    regardless of what mode was previously active.
    """
    with state.lock:
        state.dog_door_prior_mode = ""
    if state.hub:
        if state.hub.monitor_active:
            state.hub.stop_monitor()
        state.hub.disarm()


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
    _stop_current_mode()
    with state.lock:
        state.mode = "disarmed"
    send_ntfy("Alarm disarmed", tags="unlock")
    return jsonify({"ok": True, "mode": "disarmed"})


@app.route("/api/away", methods=["POST"])
def api_away():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Away: HIGH volume, AWAY mode. Hub owns siren — no monitor, no auto-rearm.
    state.hub.arm_away()
    with state.lock:
        state.mode = "away"
    send_ntfy("Alarm set to AWAY", tags="lock")
    return jsonify({"ok": True, "mode": "away"})


@app.route("/api/day", methods=["POST"])
def api_day():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Day: MUTE volume, HOME mode. No arm beep, no siren. ntfy only.
    state.hub.arm_silent()
    state.hub.start_monitor(mode="day")
    with state.lock:
        state.mode = "day"
    send_ntfy("Day monitor active", tags="eyes")
    return jsonify({"ok": True, "mode": "day"})


@app.route("/api/night", methods=["POST"])
def api_night():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Night: HIGH volume, HOME mode. Arm beep. Siren runs 30s then auto-rearm silently.
    state.hub.arm_loud()
    state.hub.start_monitor(mode="night")
    with state.lock:
        state.mode = "night"
    send_ntfy("Night monitor active", tags="moon")
    return jsonify({"ok": True, "mode": "night"})


@app.route("/api/silent_night", methods=["POST"])
def api_silent_night():
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    _stop_current_mode()
    # Silent Night: MUTE volume, HOME mode. No arm beep, no siren. ntfy only.
    state.hub.arm_silent()
    state.hub.start_monitor(mode="silent_night")
    with state.lock:
        state.mode = "silent_night"
    send_ntfy("Silent night active", tags="zzz")
    return jsonify({"ok": True, "mode": "silent_night"})


@app.route("/api/suspend", methods=["POST"])
def api_suspend():
    """
    Dog Door: mute volume only — no mode change, no beep, no siren.

    Hub stays in HOME mode with monitor running. Only the volume is changed
    to MUTE so sensor triggers are silent. Monitor mode switches to dog_door
    so the rearm sequence knows to stay silent after a trigger.
    Only available in night mode.
    """
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    with state.lock:
        current_mode = state.mode
        if current_mode != "night":
            return jsonify({"error": "Dog Door only available in Night mode"}), 400
        if state.dog_door_prior_mode:
            return jsonify({"ok": True, "mode": "dog_door"})
        state.dog_door_prior_mode = current_mode
        state.mode = "dog_door"

    # Mute only — hub stays in HOME, monitor keeps running, no DPS 101 write
    state.hub._set_dps(DPS_VOLUME, VolumeLevel.MUTE.value)
    state.hub._monitor_mode = "dog_door"

    logger.info("Dog door: volume muted, monitor mode → dog_door")
    send_ntfy("Dog door — alarm muted", tags="dog")
    return jsonify({"ok": True, "mode": "dog_door"})


@app.route("/api/suspend/cancel", methods=["POST"])
def api_suspend_cancel():
    """
    Cancel Dog Door: restore volume only — no mode change, no beep.

    Hub stays in HOME, monitor keeps running, volume restored to HIGH.
    Monitor mode switches back to night.
    """
    if not state.hub_connected:
        return jsonify({"error": "Hub not connected"}), 503
    with state.lock:
        prior_mode = state.dog_door_prior_mode
        if not prior_mode:
            return jsonify({"ok": True, "mode": state.mode})
        state.dog_door_prior_mode = ""
        state.mode = "night"

    # Restore volume only — hub stays in HOME, no DPS 101 write
    state.hub._set_dps(DPS_VOLUME, VolumeLevel.HIGH.value)
    state.hub._monitor_mode = "night"

    logger.info("Dog door cancelled — volume restored, monitor mode → night")
    send_ntfy("Dog door cancelled — Night restored", tags="lock")
    return jsonify({"ok": True, "mode": "night"})


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
    import subprocess
    with state.lock:
        uptime = None
        if state.hub_connect_time:
            delta = datetime.now() - state.hub_connect_time
            uptime = str(delta).split(".")[0]
        return jsonify({
            "hub_connected": state.hub_connected,
            "hub_ip": state.hub.ip_address if state.hub else None,
            "hub_uptime": uptime,
            "last_health_check": state.last_health_check_time,
            "monitor_running": state._monitor_running,
            "monitor_mode": state.hub._monitor_mode if state.hub else "",
            "monitor_rearming": state.hub._monitor_rearming if state.hub else False,
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
    import subprocess
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
    import subprocess
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
