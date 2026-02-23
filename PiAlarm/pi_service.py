"""
pi_service.py -- AGSHome alarm service entry point for Raspberry Pi.

Replaces tray.py for headless Linux operation. Handles:
  - mDNS advertisement (agshome.local) via zeroconf
  - Hub and camera connection at startup
  - Flask server (foreground, managed by systemd)
  - Clean shutdown on SIGTERM / SIGINT

Usage:
    python pi_service.py                    # Run directly (for testing)
    sudo systemctl start agshome           # Run via systemd (production)
"""

import logging
import os
import signal
import socket
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Ensure working directory is the script's location (for config.json)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from zeroconf import ServiceInfo, Zeroconf

from server import (
    app, state, connect_hub, connect_camera,
    start_monitor_thread, stop_monitor_thread, get_local_ip,
)

PORT = 5000
MDNS_NAME = "agshome"
_zeroconf: Zeroconf | None = None


# ============================================================
# mDNS Advertisement (agshome.local)
# ============================================================

def _register_mdns():
    """Advertise the server as agshome.local via mDNS/Avahi."""
    global _zeroconf
    local_ip = get_local_ip()
    try:
        info = ServiceInfo(
            "_http._tcp.local.",
            "AGSHome Alarm._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=PORT,
            properties={"path": "/"},
            server=f"{MDNS_NAME}.local.",
        )
        _zeroconf = Zeroconf()
        _zeroconf.register_service(info)
        logger.info(f"mDNS registered: http://{MDNS_NAME}.local:{PORT} ({local_ip})")
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
# Shutdown Handler
# ============================================================

def _shutdown(signum, frame):
    """Handle SIGTERM / SIGINT gracefully."""
    logger.info(f"Shutdown signal received ({signum}), cleaning up...")
    _unregister_mdns()
    stop_monitor_thread()
    if state.camera:
        state.camera.disconnect()
    if state.hub:
        if state.hub.monitor_active:
            state.hub.stop_monitor()
        state.hub.disconnect()
    sys.exit(0)


# ============================================================
# Main
# ============================================================

def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("AGSHome Pi Service starting...")

    _register_mdns()
    connect_hub()
    connect_camera()
    start_monitor_thread()

    local_ip = get_local_ip()
    logger.info(f"Server ready:")
    logger.info(f"  Phone:   http://{MDNS_NAME}.local:{PORT}")
    logger.info(f"  Desktop: http://{MDNS_NAME}.local:{PORT}/desktop")
    logger.info(f"  By IP:   http://{local_ip}:{PORT}")

    # Flask runs in the foreground — systemd keeps it alive
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    finally:
        _unregister_mdns()
        stop_monitor_thread()
        if state.camera:
            state.camera.disconnect()
        if state.hub:
            if state.hub.monitor_active:
                state.hub.stop_monitor()
            state.hub.disconnect()


if __name__ == "__main__":
    main()
