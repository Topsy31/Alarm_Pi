#!/usr/bin/env python3
"""
discover_camera.py - Find and test your O-KAM camera's RTSP stream.

This script will:
  1. Scan your local network for devices with open RTSP ports
  2. Probe common RTSP URL patterns to find a working stream
  3. Display a test frame and save connection details
  4. Optionally save a test snapshot

Usage:
    python discover_camera.py                          # Scan network
    python discover_camera.py --ip 192.168.1.100       # Test specific IP
    python discover_camera.py --ip 192.168.1.100 --show  # Show live preview
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"

# Ports commonly used by IP cameras for RTSP
RTSP_PORTS = [554, 8554, 10555]


def get_local_subnet() -> str:
    """Detect the local subnet (e.g. '192.168.1')."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        return ".".join(parts[:3])
    except Exception:
        return "192.168.1"


def check_port(ip: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is open on an IP address."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def scan_for_cameras(subnet: str = None) -> list[dict]:
    """
    Scan the local network for devices with open RTSP ports.

    Returns a list of dicts with ip and open ports.
    """
    if subnet is None:
        subnet = get_local_subnet()

    print(f"\nScanning {subnet}.1-254 for RTSP-capable devices...")
    print(f"Checking ports: {RTSP_PORTS}\n")

    found = []

    def check_host(ip):
        open_ports = []
        for port in RTSP_PORTS:
            if check_port(ip, port):
                open_ports.append(port)
        if open_ports:
            return {"ip": ip, "ports": open_ports}
        return None

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {
            executor.submit(check_host, f"{subnet}.{i}"): i
            for i in range(1, 255)
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                found.append(result)
                print(f"  Found: {result['ip']} - open ports: {result['ports']}")

    if not found:
        print("  No devices with open RTSP ports found.")
        print("  Your camera may need to be powered on and connected to Wi-Fi.")
    else:
        print(f"\n  Found {len(found)} potential camera(s).")

    return found


def probe_camera(ip: str) -> dict | None:
    """Probe a camera IP using the camera module."""
    from agshome.camera import probe_camera_rtsp
    return probe_camera_rtsp(ip)


def show_live_preview(rtsp_url: str, camera_name: str = "O-KAM Camera"):
    """Show a live preview window. Press Q to close."""
    try:
        import cv2
    except ImportError:
        print("OpenCV required for live preview. Install: pip install opencv-python")
        return

    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)

    if not cap.isOpened():
        print(f"Could not open stream: {rtsp_url}")
        return

    print(f"\nShowing live preview. Press Q to close.\n")

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Stream ended or failed.")
            break

        cv2.imshow(camera_name, frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


def save_camera_config(ip: str, port: int, path: str):
    """Save discovered camera details to config.json."""
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            config = json.load(f)

    config["camera"] = {
        "name": "O-KAM Camera",
        "ip_address": ip,
        "rtsp_port": port,
        "stream_path": path,
        "sub_stream_path": path.replace("av0_0", "av0_1") if "av0_0" in path else path,
        "username": "",
        "password": "",
    }

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nCamera config saved to {CONFIG_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Discover and test your O-KAM camera"
    )
    parser.add_argument(
        "--ip", type=str, default=None,
        help="Camera IP address (skip network scan)"
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Show live preview window"
    )
    parser.add_argument(
        "--snapshot", type=str, default=None,
        help="Save a test snapshot to this file"
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  O-KAM Camera Discovery Tool")
    print("=" * 60)

    try:
        import cv2
        print(f"  OpenCV version: {cv2.__version__}")
    except ImportError:
        print("\n  WARNING: OpenCV not installed!")
        print("  Install with: pip install opencv-python")
        print("  Network scan will still work, but stream testing won't.\n")

    # Step 1: Find the camera
    target_ip = args.ip
    target_port = None
    target_path = None

    if not target_ip:
        # Scan the network
        devices = scan_for_cameras()
        if not devices:
            print("\nNo cameras found automatically.")
            print("If you know the camera IP, try: python discover_camera.py --ip <IP>")
            print("\nTip: Check the O-KAM Pro app for your camera's IP address,")
            print("or look at your router's connected devices list.")
            sys.exit(1)

        if len(devices) == 1:
            target_ip = devices[0]["ip"]
            print(f"\nUsing discovered camera at {target_ip}")
        else:
            print("\nMultiple devices found. Enter the IP to test:")
            for i, d in enumerate(devices):
                print(f"  [{i + 1}] {d['ip']} (ports: {d['ports']})")
            choice = input("\nChoice (number or IP): ").strip()
            try:
                idx = int(choice) - 1
                target_ip = devices[idx]["ip"]
            except (ValueError, IndexError):
                target_ip = choice

    # Step 2: Probe RTSP URLs
    print(f"\nProbing RTSP streams on {target_ip}...")

    try:
        result = probe_camera(target_ip)
    except ImportError:
        # cv2 not available, just report open ports
        print("\nOpenCV not available - checking open ports only:")
        for port in RTSP_PORTS:
            status = "OPEN" if check_port(target_ip, port) else "closed"
            print(f"  Port {port}: {status}")
        print("\nInstall OpenCV to test actual stream connectivity:")
        print("  pip install opencv-python")
        sys.exit(0)

    if result:
        print(f"\n  SUCCESS: Found working stream!")
        print(f"  URL: {result['url']}")
        print(f"  Type: {result['description']}")

        target_port = result["port"]
        target_path = result["path"]

        # Save config
        save_camera_config(target_ip, target_port, target_path)

        # Take test snapshot
        if args.snapshot:
            import cv2
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
            cap = cv2.VideoCapture(result["url"], cv2.CAP_FFMPEG)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    cv2.imwrite(args.snapshot, frame)
                    print(f"  Snapshot saved: {args.snapshot}")
                cap.release()

        # Show live preview
        if args.show:
            show_live_preview(result["url"])

    else:
        print("\n  No working RTSP stream found with standard URL patterns.")
        print("\n  Possible reasons:")
        print("  - Camera firmware may encrypt/block local RTSP")
        print("  - Camera may use non-standard port or URL format")
        print("  - Camera may require authentication")
        print("\n  Try these steps:")
        print("  1. Check if the camera has RTSP settings in the O-KAM app")
        print("  2. Try with credentials: --ip <IP> (then edit config.json)")
        print("  3. Use ONVIF Device Manager (Windows) to discover the URL")
        print("  4. Contact O-KAM support for RTSP details")

    print("\nDone!")


if __name__ == "__main__":
    main()
