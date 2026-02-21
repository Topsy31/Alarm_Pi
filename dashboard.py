#!/usr/bin/env python3
"""
dashboard.py - Unified security dashboard for AGSHome hub + O-KAM camera.

Combines alarm status monitoring with live camera feed in a single
tkinter GUI window. Features:

  - Live camera feed with motion detection overlay
  - Alarm status panel with arm/disarm controls
  - Sensor state indicators
  - Event log with timestamps
  - Snapshot-on-alarm: auto-captures camera frame when alarm triggers

Usage:
    python dashboard.py                 # Launch GUI dashboard
    python dashboard.py --headless      # Run in headless mode (log only)
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
DEVICES_FILE = "devices.json"
SNAPSHOT_DIR = "snapshots"

# Try importing GUI and camera dependencies
try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    TK_AVAILABLE = True
except ImportError:
    TK_AVAILABLE = False

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageTk
    CAMERA_AVAILABLE = True
except ImportError:
    CAMERA_AVAILABLE = False

from agshome.hub import AGSHomeHub
from agshome.dps_map import AlarmMode
from agshome.camera import OKamCamera, CameraConfig


def load_config() -> dict:
    """Load the combined config file."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def create_hub(config: dict) -> Optional[AGSHomeHub]:
    """Create and connect to the alarm hub."""
    hc = config.get("hub", {})
    if not hc.get("device_id") or hc["device_id"].startswith("YOUR"):
        # Try devices.json fallback
        if os.path.exists(DEVICES_FILE):
            with open(DEVICES_FILE) as f:
                devices = json.load(f)
            if devices:
                d = devices[0]
                hc = {
                    "device_id": d["id"],
                    "ip_address": d.get("ip", ""),
                    "local_key": d["key"],
                    "protocol_version": float(d.get("version", 3.3)),
                }

    if not hc.get("device_id"):
        return None

    hub = AGSHomeHub(
        device_id=hc["device_id"],
        ip_address=hc["ip_address"],
        local_key=hc["local_key"],
        version=hc.get("protocol_version", 3.3),
    )
    return hub


def create_camera(config: dict) -> Optional[OKamCamera]:
    """Create and configure the camera."""
    cc = config.get("camera", {})
    if not cc.get("ip_address"):
        return None

    cam_config = CameraConfig(
        name=cc.get("name", "O-KAM Camera"),
        ip_address=cc["ip_address"],
        rtsp_port=cc.get("rtsp_port", 10555),
        username=cc.get("username", ""),
        password=cc.get("password", ""),
        stream_path=cc.get("stream_path", "TCP/av0_0"),
        sub_stream_path=cc.get("sub_stream_path", "TCP/av0_1"),
        use_sub_stream=cc.get("use_sub_stream", False),
    )
    return OKamCamera(cam_config)


# ============================================================
# Headless Mode (no GUI - just logging)
# ============================================================

def run_headless(hub: Optional[AGSHomeHub], camera: Optional[OKamCamera]):
    """Run in headless mode - monitor and log events."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)

    if hub:
        if hub.connect():
            logger.info("Hub connected")
        else:
            logger.error("Hub connection failed")
            hub = None

    if camera:
        if camera.connect():
            logger.info("Camera connected")
        else:
            logger.error("Camera connection failed")
            camera = None

    if not hub and not camera:
        logger.error("No devices connected. Exiting.")
        return

    def on_alarm_change(dps_index, new_value, old_value):
        logger.info(f"ALARM DPS {dps_index}: {old_value} -> {new_value}")
        # Auto-snapshot on alarm trigger
        if camera and camera.is_connected():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SNAPSHOT_DIR, f"alarm_{timestamp}.jpg")
            camera.snapshot(save_path=path)
            logger.info(f"Alarm snapshot saved: {path}")

    def on_motion(score, frame):
        if score > 15:  # Significant motion
            logger.info(f"Motion detected! Score: {score:.1f}")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(SNAPSHOT_DIR, f"motion_{timestamp}.jpg")
            cv2.imwrite(path, frame)

    if hub:
        hub.add_listener(on_alarm_change)

    if camera:
        camera.add_motion_listener(on_motion)
        camera.start_stream(display=False, fps_limit=5)

    logger.info("Monitoring started (Ctrl+C to stop)...")

    try:
        if hub:
            hub.poll_loop(interval=5.0)
        else:
            # Camera only - just wait
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        if camera:
            camera.disconnect()
        if hub:
            hub.disconnect()


# ============================================================
# GUI Dashboard
# ============================================================

class SecurityDashboard:
    """Tkinter-based security dashboard combining alarm + camera."""

    POLL_INTERVAL_MS = 3000      # Alarm status poll interval
    CAMERA_FRAME_MS = 66         # ~15 FPS camera update
    CAMERA_DISPLAY_WIDTH = 640
    CAMERA_DISPLAY_HEIGHT = 480

    def __init__(self, hub: Optional[AGSHomeHub], camera: Optional[OKamCamera]):
        self.hub = hub
        self.camera = camera
        self.running = False
        self._hub_connected = False
        self._camera_connected = False
        self._last_hub_status = {}

        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        self._build_gui()

    def _build_gui(self):
        """Build the main GUI window."""
        self.root = tk.Tk()
        self.root.title("AGSHome Security Dashboard")
        self.root.geometry("1000x700")
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- Main layout: left (camera) + right (controls) ---
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Left: Camera feed
        left_frame = ttk.LabelFrame(main_frame, text="Camera Feed")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        self.camera_label = ttk.Label(left_frame, text="No camera connected")
        self.camera_label.pack(fill=tk.BOTH, expand=True)

        cam_btn_frame = ttk.Frame(left_frame)
        cam_btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(cam_btn_frame, text="📸 Snapshot",
                   command=self._take_snapshot).pack(side=tk.LEFT, padx=2)

        # Right: Alarm controls + log
        right_frame = ttk.Frame(main_frame, width=320)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(5, 0))
        right_frame.pack_propagate(False)

        # Alarm status
        status_frame = ttk.LabelFrame(right_frame, text="Alarm Status")
        status_frame.pack(fill=tk.X, pady=(0, 5))

        self.status_var = tk.StringVar(value="Connecting...")
        ttk.Label(status_frame, textvariable=self.status_var,
                  font=("Helvetica", 14, "bold")).pack(pady=10)

        self.hub_status_label = ttk.Label(status_frame, text="Hub: --")
        self.hub_status_label.pack()
        self.camera_status_label = ttk.Label(status_frame, text="Camera: --")
        self.camera_status_label.pack(pady=(0, 5))

        # Alarm controls
        ctrl_frame = ttk.LabelFrame(right_frame, text="Alarm Control")
        ctrl_frame.pack(fill=tk.X, pady=5)

        btn_style = {"width": 20}
        ttk.Button(ctrl_frame, text="🔒 ARM AWAY",
                   command=lambda: self._set_mode("away"),
                   **btn_style).pack(pady=2, padx=10)
        ttk.Button(ctrl_frame, text="🏠 ARM HOME",
                   command=lambda: self._set_mode("home"),
                   **btn_style).pack(pady=2, padx=10)
        ttk.Button(ctrl_frame, text="🔓 DISARM",
                   command=lambda: self._set_mode("disarmed"),
                   **btn_style).pack(pady=2, padx=10)
        ttk.Button(ctrl_frame, text="🔔 TEST SIREN",
                   command=self._test_siren,
                   **btn_style).pack(pady=(10, 2), padx=10)
        ttk.Button(ctrl_frame, text="🔕 SILENCE SIREN",
                   command=self._silence_siren,
                   **btn_style).pack(pady=(2, 10), padx=10)

        # Event log
        log_frame = ttk.LabelFrame(right_frame, text="Event Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, width=35, font=("Consolas", 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _log_event(self, message: str):
        """Add a timestamped event to the log panel."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def start(self):
        """Connect devices and start the dashboard."""
        self.running = True
        self._log_event("Dashboard starting...")

        # Connect hub in background thread
        if self.hub:
            threading.Thread(target=self._connect_hub, daemon=True).start()
        else:
            self._log_event("No hub configured")

        # Connect camera in background thread
        if self.camera and CAMERA_AVAILABLE:
            threading.Thread(target=self._connect_camera, daemon=True).start()
        elif not CAMERA_AVAILABLE:
            self._log_event("Camera deps missing (pip install opencv-python Pillow)")
        else:
            self._log_event("No camera configured")

        # Start periodic updates
        self.root.after(self.POLL_INTERVAL_MS, self._poll_hub)
        self.root.after(self.CAMERA_FRAME_MS, self._update_camera_frame)

        self._log_event("Dashboard ready")
        self.root.mainloop()

    def _connect_hub(self):
        """Connect to the alarm hub (background thread)."""
        self.root.after(0, lambda: self._log_event("Connecting to hub..."))
        if self.hub.connect():
            self._hub_connected = True
            self.root.after(0, lambda: self._log_event("Hub connected!"))
            self.root.after(0, lambda: self.hub_status_label.config(
                text="Hub: Connected ✓"
            ))

            # Add alarm change listener
            def on_change(idx, new_val, old_val):
                msg = f"DPS {idx}: {old_val} -> {new_val}"
                self.root.after(0, lambda m=msg: self._log_event(m))
                # Auto-snapshot
                if self._camera_connected and self.camera:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join(SNAPSHOT_DIR, f"alarm_{ts}.jpg")
                    self.camera.snapshot(save_path=path)
                    self.root.after(
                        0, lambda p=path: self._log_event(f"Snapshot: {p}")
                    )

            self.hub.add_listener(on_change)
        else:
            self.root.after(0, lambda: self._log_event("Hub connection failed"))
            self.root.after(0, lambda: self.hub_status_label.config(
                text="Hub: Disconnected ✗"
            ))

    def _connect_camera(self):
        """Connect to the camera (background thread)."""
        self.root.after(0, lambda: self._log_event("Connecting to camera..."))
        if self.camera.connect():
            self._camera_connected = True
            self.root.after(0, lambda: self._log_event("Camera connected!"))
            self.root.after(0, lambda: self.camera_status_label.config(
                text="Camera: Connected ✓"
            ))
        else:
            self.root.after(0, lambda: self._log_event("Camera connection failed"))
            self.root.after(0, lambda: self.camera_status_label.config(
                text="Camera: Disconnected ✗"
            ))

    def _poll_hub(self):
        """Periodically poll the alarm hub for status changes."""
        if not self.running:
            return

        if self._hub_connected and self.hub:
            try:
                status = self.hub.status()
                if "error" not in status:
                    # Detect changes
                    for idx, val in status.items():
                        old = self._last_hub_status.get(idx)
                        if old is not None and old != val:
                            self._log_event(f"DPS {idx}: {old} -> {val}")
                    self._last_hub_status = dict(status)

                    # Update mode display
                    mode = status.get("2", status.get(2, "unknown"))
                    self.status_var.set(f"Mode: {str(mode).upper()}")
            except Exception as e:
                self._log_event(f"Hub poll error: {e}")

        self.root.after(self.POLL_INTERVAL_MS, self._poll_hub)

    def _update_camera_frame(self):
        """Update the camera feed display."""
        if not self.running:
            return

        if self._camera_connected and self.camera and CAMERA_AVAILABLE:
            frame = self.camera.read_frame()
            if frame is not None:
                # Resize for display
                frame = cv2.resize(
                    frame,
                    (self.CAMERA_DISPLAY_WIDTH, self.CAMERA_DISPLAY_HEIGHT)
                )
                # Add timestamp overlay
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cv2.putText(
                    frame, ts, (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1
                )
                # Convert BGR -> RGB -> PIL -> Tk
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                self.camera_label.imgtk = imgtk  # Keep reference
                self.camera_label.configure(image=imgtk)

        self.root.after(self.CAMERA_FRAME_MS, self._update_camera_frame)

    # --- Button handlers ---

    def _set_mode(self, mode: str):
        """Set alarm mode from button click."""
        if not self._hub_connected:
            self._log_event("Hub not connected!")
            return

        mode_map = {
            "away": AlarmMode.AWAY,
            "home": AlarmMode.HOME,
            "disarmed": AlarmMode.DISARMED,
        }
        self._log_event(f"Setting mode: {mode}")
        threading.Thread(
            target=lambda: self.hub.set_mode(mode_map[mode]),
            daemon=True,
        ).start()

    def _test_siren(self):
        if not self._hub_connected:
            return
        self._log_event("Testing siren...")
        threading.Thread(
            target=lambda: self.hub.trigger_siren(True),
            daemon=True,
        ).start()

    def _silence_siren(self):
        if not self._hub_connected:
            return
        self._log_event("Silencing siren...")
        threading.Thread(
            target=lambda: self.hub.trigger_siren(False),
            daemon=True,
        ).start()

    def _take_snapshot(self):
        if not self._camera_connected:
            self._log_event("Camera not connected!")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SNAPSHOT_DIR, f"manual_{ts}.jpg")
        result = self.camera.snapshot(save_path=path)
        if result:
            self._log_event(f"Snapshot saved: {path}")

    def _on_close(self):
        """Clean shutdown."""
        self.running = False
        if self.camera:
            self.camera.disconnect()
        if self.hub:
            self.hub.disconnect()
        self.root.destroy()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Unified security dashboard"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run without GUI (logging/snapshots only)"
    )
    args = parser.parse_args()

    config = load_config()
    hub = create_hub(config)
    camera = create_camera(config)

    if not hub and not camera:
        print("No devices configured!")
        print("Run discover.py (for hub) and discover_camera.py (for camera) first.")
        sys.exit(1)

    if args.headless or not TK_AVAILABLE:
        if not TK_AVAILABLE and not args.headless:
            print("tkinter not available. Running in headless mode.")
        run_headless(hub, camera)
    else:
        dashboard = SecurityDashboard(hub, camera)
        dashboard.start()


if __name__ == "__main__":
    main()
