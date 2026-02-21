"""
tray.py -- System tray launcher for the AGSHome mobile alarm server.

Runs the Flask web server and hub monitor loop as background threads,
with a Windows system tray icon for status and control.

Usage:
    python tray.py          # Launch with console (for debugging)
    pythonw tray.py         # Launch without console window
"""

import logging
import os
import socket
import sys
import threading
import webbrowser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Ensure working directory is the script's location (for config.json)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem, Menu
from zeroconf import ServiceInfo, Zeroconf

from server import (
    app, state, connect_hub, start_monitor_thread,
    stop_monitor_thread, get_local_ip,
)

PORT = 5000
MDNS_NAME = "agshome"
_zeroconf: Zeroconf | None = None


# ============================================================
# Tray Icon
# ============================================================

def create_icon(connected: bool = False) -> Image.Image:
    """Create a simple tray icon — green if connected, grey if not."""
    size = 64
    colour = (68, 204, 68) if connected else (136, 136, 136)
    img = Image.new("RGB", (size, size), colour)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except (OSError, IOError):
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "A", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2 - 2), "A", fill="white", font=font)
    return img


# ============================================================
# Menu Actions
# ============================================================

def on_open_browser(icon, item):
    """Open the dashboard in the default browser."""
    webbrowser.open(f"http://{MDNS_NAME}.local:{PORT}")


def on_quit(icon, item):
    """Shut everything down."""
    logger.info("Shutting down...")
    _unregister_mdns()
    stop_monitor_thread()
    if state.hub:
        if state.hub.monitor_active:
            state.hub.stop_monitor()
        state.hub.disconnect()
    icon.stop()


# ============================================================
# Auto-start (Windows shell:startup)
# ============================================================

def _startup_shortcut_path() -> str:
    """Return the path for a Windows startup shortcut."""
    startup = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )
    return os.path.join(startup, "AGSHome Alarm.bat")


def _is_auto_start() -> bool:
    return os.path.exists(_startup_shortcut_path())


def toggle_auto_start(icon, item):
    """Create or remove the startup batch file."""
    path = _startup_shortcut_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info("Auto-start disabled")
    else:
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        script = os.path.abspath(__file__)
        workdir = os.path.dirname(script)
        with open(path, "w") as f:
            f.write(f'@start "" /D "{workdir}" "{pythonw}" "{script}"\n')
        logger.info(f"Auto-start enabled: {path}")


# ============================================================
# mDNS Advertisement (agshome.local)
# ============================================================

def _register_mdns():
    """Advertise the server as agshome.local via mDNS."""
    global _zeroconf
    local_ip = get_local_ip()
    try:
        info = ServiceInfo(
            "_http._tcp.local.",
            f"AGSHome Alarm._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=PORT,
            properties={"path": "/"},
            server=f"{MDNS_NAME}.local.",
        )
        _zeroconf = Zeroconf()
        _zeroconf.register_service(info)
        logger.info(f"mDNS registered: http://{MDNS_NAME}.local:{PORT}")
    except Exception as e:
        logger.warning(f"mDNS registration failed: {e}")


def _unregister_mdns():
    """Remove the mDNS advertisement."""
    global _zeroconf
    if _zeroconf:
        try:
            _zeroconf.unregister_all_services()
            _zeroconf.close()
        except Exception:
            pass
        _zeroconf = None


# ============================================================
# Startup
# ============================================================

def setup(icon):
    """Called after the tray icon is visible. Start all services."""
    icon.visible = True

    def _startup():
        _register_mdns()
        connect_hub()
        if state.hub_connected:
            icon.icon = create_icon(connected=True)
            icon.title = f"AGSHome — connected ({state.hub.ip_address})"
        else:
            icon.title = "AGSHome — connection failed"
        start_monitor_thread()
        logger.info(f"Server ready at http://{MDNS_NAME}.local:{PORT}")

    threading.Thread(target=_startup, daemon=True).start()

    # Flask server in daemon thread
    threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0", port=PORT,
            debug=False, use_reloader=False,
        ),
        daemon=True,
    ).start()


def build_menu():
    """Build the tray icon context menu."""
    return Menu(
        MenuItem(f"http://{MDNS_NAME}.local:{PORT}", on_open_browser, default=True),
        Menu.SEPARATOR,
        MenuItem(
            "Start with Windows",
            toggle_auto_start,
            checked=lambda item: _is_auto_start(),
        ),
        Menu.SEPARATOR,
        MenuItem("Quit", on_quit),
    )


# ============================================================
# Main
# ============================================================

def main():
    icon = pystray.Icon(
        name="AGSHome",
        icon=create_icon(connected=False),
        title="AGSHome — starting...",
        menu=build_menu(),
    )
    icon.run(setup=setup)


if __name__ == "__main__":
    main()
