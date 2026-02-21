#!/usr/bin/env python3
"""
dashboard.py — AGSHome alarm hub dashboard.

Tkinter GUI for monitoring and controlling the AGSHome alarm hub
over the local network. Features:

  - Live alarm status display (away / home / disarmed)
  - Arm/disarm control buttons
  - Sensor event feed with decoded sensor names
  - Hub settings display (zones, volume, delay)
  - Headless mode for background monitoring

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
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
DEVICES_FILE = "devices.json"

try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    TK_AVAILABLE = True
except ImportError:
    TK_AVAILABLE = False

from agshome.hub import AGSHomeHub
from agshome.dps_map import (
    AlarmMode, VolumeLevel, MODE_LABELS, VOLUME_LABELS,
    DPS_ALARM_MODE, DPS_ALARM_TRIGGERED, DPS_SIREN,
    DPS_ALARM_DURATION, DPS_ALARM_TONE, DPS_VOLUME,
    DPS_ZONE_1_ENABLED, DPS_ZONE_2_ENABLED,
    DPS_SENSOR_EVENT, DPS_NOTIFICATION,
    describe_dps, decode_utf16_base64,
)


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
        # Fallback to devices.json
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
# Headless Mode
# ============================================================

def run_headless(hub: AGSHomeHub):
    """Run in headless mode — monitor and log events."""
    if hub.connect():
        logger.info("Hub connected")
    else:
        logger.error("Hub connection failed")
        return

    def on_change(idx, new_val, old_val):
        desc = describe_dps(idx, new_val)
        logger.info(f"CHANGE: {desc} (was: {old_val})")

    hub.add_listener(on_change)
    logger.info("Monitoring started (Ctrl+C to stop)...")

    try:
        hub.poll_loop(interval=5.0)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        hub.disconnect()


# ============================================================
# GUI Dashboard
# ============================================================

# Colour scheme
BG_DARK = "#1a1a2e"
BG_PANEL = "#16213e"
BG_CARD = "#0f3460"
FG_TEXT = "#e0e0e0"
FG_DIM = "#8892a0"
FG_ACCENT = "#00d2ff"
COL_ARMED = "#ff4444"
COL_HOME = "#ffaa00"
COL_DISARMED = "#44cc44"
COL_TRIGGERED = "#ff0000"
COL_SENSOR = "#ff6600"
COL_MONITOR = "#6644cc"


class SecurityDashboard:
    """Tkinter-based alarm hub dashboard."""

    POLL_INTERVAL_MS = 2000
    ASYNC_CHECK_MS = 500

    # Known sensors (from user)
    SENSORS = [
        "Livingroom Door",
        "Livingroom Window",
        "Office Window",
        "Side Door",
        "Kitchen Door",
    ]

    def __init__(self, hub: AGSHomeHub):
        self.hub = hub
        self.running = False
        self._hub_connected = False
        self._last_status = {}
        self._build_gui()

    def _build_gui(self):
        """Build the main GUI window."""
        self.root = tk.Tk()
        self.root.title("AGSHome Security Dashboard")
        self.root.geometry("750x600")
        self.root.minsize(650, 500)
        self.root.configure(bg=BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # --- Top: Alarm mode banner ---
        self.mode_frame = tk.Frame(self.root, bg=COL_DISARMED, height=70)
        self.mode_frame.pack(fill=tk.X, padx=10, pady=(10, 5))
        self.mode_frame.pack_propagate(False)

        self.mode_label = tk.Label(
            self.mode_frame, text="CONNECTING...",
            font=("Helvetica", 24, "bold"), fg="white", bg=COL_DISARMED,
        )
        self.mode_label.pack(expand=True)

        self.triggered_label = tk.Label(
            self.mode_frame, text="",
            font=("Helvetica", 11), fg="white", bg=COL_DISARMED,
        )
        self.triggered_label.pack()

        # --- Middle: two-column layout ---
        mid_frame = tk.Frame(self.root, bg=BG_DARK)
        mid_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Left column: controls + sensors
        left_col = tk.Frame(mid_frame, bg=BG_DARK)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        # Arm/disarm buttons
        ctrl_frame = tk.LabelFrame(
            left_col, text=" Alarm Control ", fg=FG_ACCENT, bg=BG_PANEL,
            font=("Helvetica", 10, "bold"),
        )
        ctrl_frame.pack(fill=tk.X, pady=(0, 5))

        btn_cfg = {"font": ("Helvetica", 11, "bold"), "width": 18, "height": 2, "relief": "flat", "cursor": "hand2"}

        self.btn_away = tk.Button(
            ctrl_frame, text="ARM AWAY", bg="#cc3333", fg="white",
            command=lambda: self._set_mode(AlarmMode.AWAY), **btn_cfg,
        )
        self.btn_away.pack(pady=(8, 3), padx=10)

        self.btn_home = tk.Button(
            ctrl_frame, text="ARM HOME", bg="#cc8800", fg="white",
            command=lambda: self._set_mode(AlarmMode.HOME), **btn_cfg,
        )
        self.btn_home.pack(pady=3, padx=10)

        self.btn_disarm = tk.Button(
            ctrl_frame, text="DISARM", bg="#339933", fg="white",
            command=lambda: self._set_mode(AlarmMode.DISARMED), **btn_cfg,
        )
        self.btn_disarm.pack(pady=3, padx=10)

        # Separator
        tk.Frame(ctrl_frame, bg=FG_DIM, height=1).pack(fill=tk.X, padx=10, pady=5)

        self.btn_monitor = tk.Button(
            ctrl_frame, text="MONITOR", bg=COL_MONITOR, fg="white",
            command=lambda: self._toggle_monitor(muted=False), **btn_cfg,
        )
        self.btn_monitor.pack(pady=(3, 3), padx=10)

        self.btn_day_monitor = tk.Button(
            ctrl_frame, text="DAY MONITOR", bg="#336655", fg="white",
            command=lambda: self._toggle_monitor(muted=True), **btn_cfg,
        )
        self.btn_day_monitor.pack(pady=(3, 3), padx=10)

        self._night_light_on = False
        self.btn_night_light = tk.Button(
            ctrl_frame, text="NIGHT LIGHT", bg="#444466", fg="white",
            command=self._toggle_night_light, **btn_cfg,
        )
        self.btn_night_light.pack(pady=(3, 8), padx=10)

        # Sensor indicators
        sensor_frame = tk.LabelFrame(
            left_col, text=" Sensors ", fg=FG_ACCENT, bg=BG_PANEL,
            font=("Helvetica", 10, "bold"),
        )
        sensor_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.sensor_indicators = {}
        for name in self.SENSORS:
            row = tk.Frame(sensor_frame, bg=BG_PANEL)
            row.pack(fill=tk.X, padx=10, pady=2)

            dot = tk.Label(row, text="●", fg=FG_DIM, bg=BG_PANEL, font=("Helvetica", 12))
            dot.pack(side=tk.LEFT, padx=(0, 8))

            label = tk.Label(row, text=name, fg=FG_TEXT, bg=BG_PANEL, font=("Helvetica", 10), anchor="w")
            label.pack(side=tk.LEFT, fill=tk.X, expand=True)

            status = tk.Label(row, text="Idle", fg=FG_DIM, bg=BG_PANEL, font=("Helvetica", 9))
            status.pack(side=tk.RIGHT)

            self.sensor_indicators[name] = {"dot": dot, "status": status, "row": row}

        # Hub settings
        settings_frame = tk.LabelFrame(
            left_col, text=" Settings ", fg=FG_ACCENT, bg=BG_PANEL,
            font=("Helvetica", 10, "bold"),
        )
        settings_frame.pack(fill=tk.X, pady=5)

        self.settings_labels = {}
        for key, label in [("volume", "Volume"), ("tone", "Alarm Tone"), ("duration", "Alarm Duration")]:
            row = tk.Frame(settings_frame, bg=BG_PANEL)
            row.pack(fill=tk.X, padx=10, pady=1)
            tk.Label(row, text=f"{label}:", fg=FG_DIM, bg=BG_PANEL, font=("Helvetica", 9), width=14, anchor="w").pack(side=tk.LEFT)
            val_label = tk.Label(row, text="--", fg=FG_TEXT, bg=BG_PANEL, font=("Helvetica", 9))
            val_label.pack(side=tk.LEFT)
            self.settings_labels[key] = val_label

        # Right column: event log
        right_col = tk.Frame(mid_frame, bg=BG_DARK, width=300)
        right_col.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        right_col.pack_propagate(False)

        log_frame = tk.LabelFrame(
            right_col, text=" Event Log ", fg=FG_ACCENT, bg=BG_PANEL,
            font=("Helvetica", 10, "bold"),
        )
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, font=("Consolas", 9), bg="#0a0a1a", fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Tag colours for log entries
        self.log_text.tag_configure("sensor", foreground=COL_SENSOR)
        self.log_text.tag_configure("alarm", foreground=COL_TRIGGERED)
        self.log_text.tag_configure("mode", foreground=FG_ACCENT)
        self.log_text.tag_configure("info", foreground=FG_DIM)

        # --- Bottom: connection status bar ---
        status_bar = tk.Frame(self.root, bg=BG_PANEL, height=25)
        status_bar.pack(fill=tk.X, padx=10, pady=(0, 10))

        self.conn_label = tk.Label(
            status_bar, text="Disconnected", fg=FG_DIM, bg=BG_PANEL,
            font=("Helvetica", 9),
        )
        self.conn_label.pack(side=tk.LEFT, padx=10, pady=3)

        self.ip_label = tk.Label(
            status_bar, text="", fg=FG_DIM, bg=BG_PANEL,
            font=("Helvetica", 9),
        )
        self.ip_label.pack(side=tk.RIGHT, padx=10, pady=3)

    def _log_event(self, message: str, tag: str = "info"):
        """Add a timestamped event to the log panel."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] ", "info")
        self.log_text.insert(tk.END, f"{message}\n", tag)
        self.log_text.see(tk.END)

    # --- Mode banner ---

    def _update_mode_display(self, mode_value: str):
        """Update the top banner to reflect the current alarm mode."""
        # Don't overwrite banner if monitor mode is active
        if self.hub.monitor_active:
            return

        try:
            mode = AlarmMode(str(mode_value))
            label = MODE_LABELS[mode]
            colour = {
                AlarmMode.AWAY: COL_ARMED,
                AlarmMode.HOME: COL_HOME,
                AlarmMode.DISARMED: COL_DISARMED,
            }[mode]
        except (ValueError, KeyError):
            label = f"UNKNOWN ({mode_value})"
            colour = FG_DIM

        self.mode_frame.configure(bg=colour)
        self.mode_label.configure(text=label, bg=colour)
        self.triggered_label.configure(bg=colour)

    def _update_triggered_display(self, triggered: bool):
        """Show/hide the alarm triggered indicator."""
        if triggered:
            # In monitor mode, show a subdued triggered state (don't go full red)
            if self.hub.monitor_active:
                self.triggered_label.configure(text="Sensor triggered — handling...", fg="white")
            else:
                self.triggered_label.configure(text="ALARM TRIGGERED!", fg="white")
                self.mode_frame.configure(bg=COL_TRIGGERED)
                self.mode_label.configure(bg=COL_TRIGGERED)
                self.triggered_label.configure(bg=COL_TRIGGERED)
        else:
            self.triggered_label.configure(text="")
            # Restore monitor banner if still in monitor mode
            if self.hub.monitor_active:
                self._update_monitor_button(True, muted=self.hub.monitor_muted)

    # --- Sensor indicators ---

    def _flash_sensor(self, sensor_name: str):
        """Briefly highlight a sensor that has triggered."""
        for name, widgets in self.sensor_indicators.items():
            if name.lower() in sensor_name.lower():
                widgets["dot"].configure(fg=COL_SENSOR)
                widgets["status"].configure(text="TRIGGERED", fg=COL_SENSOR)
                # Reset after 10 seconds
                self.root.after(10000, lambda w=widgets: self._reset_sensor(w))
                break

    def _reset_sensor(self, widgets: dict):
        """Reset a sensor indicator to idle."""
        widgets["dot"].configure(fg=FG_DIM)
        widgets["status"].configure(text="Idle", fg=FG_DIM)

    # --- Settings display ---

    def _update_settings(self, status: dict):
        """Update the settings panel from hub status."""
        vol = status.get(DPS_VOLUME, "--")
        try:
            vol_label = VOLUME_LABELS[VolumeLevel(str(vol))]
        except (ValueError, KeyError):
            vol_label = str(vol)
        self.settings_labels["volume"].configure(text=vol_label)

        tone = status.get(DPS_ALARM_TONE, "--")
        self.settings_labels["tone"].configure(text=str(tone))

        dur = status.get(DPS_ALARM_DURATION, "--")
        self.settings_labels["duration"].configure(text=str(dur))

    # --- Connection and polling ---

    def start(self):
        """Connect to hub and start the dashboard."""
        self.running = True
        self._log_event("Dashboard starting...")

        if self.hub:
            self.ip_label.configure(text=f"Hub: {self.hub.ip_address}")
            threading.Thread(target=self._connect_hub, daemon=True).start()
        else:
            self._log_event("No hub configured")

        self.root.after(self.POLL_INTERVAL_MS, self._poll_hub)
        self.root.after(self.ASYNC_CHECK_MS, self._check_async)

        self.root.mainloop()

    def _connect_hub(self):
        """Connect to the alarm hub (background thread)."""
        original_ip = self.hub.ip_address
        self.root.after(0, lambda: self._log_event(f"Connecting to hub at {original_ip}..."))

        if self.hub.connect():
            self._hub_connected = True
            # Check if IP changed via discovery
            if self.hub.ip_address != original_ip:
                self.root.after(0, lambda: self._log_event(
                    f"Hub IP changed: {original_ip} → {self.hub.ip_address}", "mode"))
                self.root.after(0, lambda: self.ip_label.configure(
                    text=f"Hub: {self.hub.ip_address}"))
            self.root.after(0, lambda: self._log_event("Hub connected!", "mode"))
            self.root.after(0, lambda: self.conn_label.configure(
                text="Connected", fg=COL_DISARMED,
            ))

            # Read initial status
            status = self.hub.status()
            if "error" not in status:
                self._last_status = dict(status)
                mode = status.get(DPS_ALARM_MODE, "?")
                self.root.after(0, lambda m=mode: self._update_mode_display(m))
                self.root.after(0, lambda s=status: self._update_settings(s))

                for line in self.hub.status_pretty():
                    self.root.after(0, lambda l=line: self._log_event(l, "info"))
        else:
            self.root.after(0, lambda: self._log_event("Hub connection failed!", "alarm"))
            self.root.after(0, lambda: self.conn_label.configure(
                text="Connection failed", fg=COL_ARMED,
            ))

    def _poll_hub(self):
        """Periodically poll the hub for status changes."""
        if not self.running:
            return

        if self._hub_connected and self.hub:
            try:
                status = self.hub.status()
                if "error" not in status:
                    self._process_status_changes(status)
                    self._last_status = dict(status)
            except Exception as e:
                self._log_event(f"Poll error: {e}", "alarm")

        self.root.after(self.POLL_INTERVAL_MS, self._poll_hub)

    def _check_async(self):
        """Check for async push messages from the hub."""
        if not self.running:
            return

        if self._hub_connected and self.hub and self.hub._device:
            events = self.hub.monitor_check_async()
            for event in events:
                dps = event.get("dps", {})
                if dps:
                    self._process_async_dps(dps)

        self.root.after(self.ASYNC_CHECK_MS, self._check_async)

    def _process_status_changes(self, status: dict):
        """Detect and display changes from polled status."""
        mode = status.get(DPS_ALARM_MODE)
        if mode:
            old_mode = self._last_status.get(DPS_ALARM_MODE)
            if old_mode != mode:
                self._update_mode_display(mode)
                desc = describe_dps(DPS_ALARM_MODE, mode)
                self._log_event(desc, "mode")

        triggered = status.get(DPS_ALARM_TRIGGERED)
        if triggered is not None:
            old_triggered = self._last_status.get(DPS_ALARM_TRIGGERED)
            if old_triggered != triggered:
                self._update_triggered_display(triggered)
                if triggered:
                    self._log_event("ALARM TRIGGERED!", "alarm")
                else:
                    self._log_event("Alarm cleared", "mode")

        siren = status.get(DPS_SIREN)
        if siren is not None:
            old_siren = self._last_status.get(DPS_SIREN)
            if old_siren != siren:
                self._update_night_light_button(siren)
                if siren:
                    self._log_event("Night light ON", "info")
                else:
                    self._log_event("Night light OFF", "info")

        self._update_settings(status)

    def _process_async_dps(self, dps: dict):
        """Process async push DPS data from the hub."""
        # Sensor event
        if DPS_SENSOR_EVENT in dps:
            sensor_name = decode_utf16_base64(dps[DPS_SENSOR_EVENT])
            self._log_event(f"SENSOR: {sensor_name}", "sensor")
            self._flash_sensor(sensor_name)

        # Notification
        if DPS_NOTIFICATION in dps:
            notification = decode_utf16_base64(dps[DPS_NOTIFICATION])
            self._log_event(f"Notification: {notification}", "mode")

        # Triggered state
        if DPS_ALARM_TRIGGERED in dps:
            self._update_triggered_display(dps[DPS_ALARM_TRIGGERED])
            if dps[DPS_ALARM_TRIGGERED]:
                self._log_event("ALARM TRIGGERED!", "alarm")

        # Mode change
        if DPS_ALARM_MODE in dps:
            self._update_mode_display(dps[DPS_ALARM_MODE])

    # --- Button handlers ---

    def _set_mode(self, mode: AlarmMode):
        """Set alarm mode from button click."""
        if not self._hub_connected:
            self._log_event("Hub not connected!", "alarm")
            return

        # Exit monitor mode if active
        if self.hub.monitor_active:
            self.hub._monitor_active = False
            self._update_monitor_button(False)

        label = MODE_LABELS[mode]
        self._log_event(f"Setting mode: {label}...", "mode")
        threading.Thread(
            target=lambda: self.hub.set_mode(mode),
            daemon=True,
        ).start()

    def _toggle_monitor(self, muted: bool = False):
        """Toggle monitor mode on/off."""
        if not self._hub_connected:
            self._log_event("Hub not connected!", "alarm")
            return

        if self.hub.monitor_active:
            self._log_event("Stopping monitor mode...", "mode")
            self._update_monitor_button(False)
            threading.Thread(target=self.hub.stop_monitor, daemon=True).start()
        else:
            label = "day monitor (muted)" if muted else "monitor"
            self._log_event(f"Starting {label} mode...", "mode")
            self._update_monitor_button(True, muted=muted)

            def on_monitor_event(event_type, message):
                self.root.after(0, lambda: self._handle_monitor_event(event_type, message))

            self.hub.add_monitor_listener(on_monitor_event)
            threading.Thread(
                target=lambda: self.hub.start_monitor(muted=muted),
                daemon=True,
            ).start()

    def _handle_monitor_event(self, event_type: str, message: str):
        """Handle events from monitor mode."""
        tag_map = {
            "sensor": "sensor",
            "silence": "mode",
            "rearm": "mode",
            "info": "info",
        }
        tag = tag_map.get(event_type, "info")
        prefix = {
            "sensor": "SENSOR",
            "silence": "MONITOR",
            "rearm": "MONITOR",
            "info": "MONITOR",
        }.get(event_type, "MONITOR")
        self._log_event(f"{prefix}: {message}", tag)

    def _toggle_night_light(self):
        """Toggle the night light on/off."""
        if not self._hub_connected:
            self._log_event("Hub not connected!", "alarm")
            return

        self._night_light_on = not self._night_light_on
        state = self._night_light_on
        self._log_event(f"Night light {'ON' if state else 'OFF'}...", "info")
        self._update_night_light_button(state)
        threading.Thread(
            target=lambda: self.hub.set_night_light(state),
            daemon=True,
        ).start()

    def _update_night_light_button(self, on: bool):
        """Update the night light button appearance."""
        if on:
            self.btn_night_light.configure(text="NIGHT LIGHT (ON)", bg="#ccaa00")
            self._night_light_on = True
        else:
            self.btn_night_light.configure(text="NIGHT LIGHT", bg="#444466")
            self._night_light_on = False

    def _update_monitor_button(self, active: bool, muted: bool = False):
        """Update the monitor button appearances."""
        if active:
            if muted:
                self.btn_day_monitor.configure(text="DAY MONITOR (ON)", bg="#44aa77")
                self.btn_monitor.configure(text="MONITOR", bg=COL_MONITOR)
                self.mode_frame.configure(bg="#336655")
                self.mode_label.configure(text="DAY MONITOR", bg="#336655")
                self.triggered_label.configure(text="Silent tracking (muted)", bg="#336655")
            else:
                self.btn_monitor.configure(text="MONITOR (ON)", bg="#8866ee")
                self.btn_day_monitor.configure(text="DAY MONITOR", bg="#336655")
                self.mode_frame.configure(bg=COL_MONITOR)
                self.mode_label.configure(text="MONITOR", bg=COL_MONITOR)
                self.triggered_label.configure(text="Silent sensor tracking", bg=COL_MONITOR)
        else:
            self.btn_monitor.configure(text="MONITOR", bg=COL_MONITOR)
            self.btn_day_monitor.configure(text="DAY MONITOR", bg="#336655")

    def _on_close(self):
        """Clean shutdown."""
        self.running = False
        if self.hub:
            self.hub.disconnect()
        self.root.destroy()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AGSHome alarm hub dashboard")
    parser.add_argument(
        "--headless", action="store_true",
        help="Run without GUI (logging only)",
    )
    args = parser.parse_args()

    config = load_config()
    hub = create_hub(config)

    if not hub:
        print("No hub configured!")
        print("Run discover.py first, or check config.json / devices.json.")
        sys.exit(1)

    if args.headless or not TK_AVAILABLE:
        if not TK_AVAILABLE and not args.headless:
            print("tkinter not available. Running in headless mode.")
        run_headless(hub)
    else:
        dashboard = SecurityDashboard(hub)
        dashboard.start()


if __name__ == "__main__":
    main()
