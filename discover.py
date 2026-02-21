#!/usr/bin/env python3
"""
discover.py — Find your AGSHome alarm hub and map its data points.

This script will:
  1. Load device credentials from devices.json (TinyTuya wizard output)
  2. Connect to the hub on the local network
  3. Query and display all DPS (data point) values
  4. Save working connection details to config.json

Usage:
    python discover.py                    # Auto-detect from devices.json
    python discover.py --scan             # Scan network for Tuya devices
    python discover.py --ip 192.168.1.50  # Test a specific IP
"""

import argparse
import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEVICES_FILE = "devices.json"
CONFIG_FILE = "config.json"


def load_devices() -> list[dict]:
    """Load device list from TinyTuya wizard output."""
    if not os.path.exists(DEVICES_FILE):
        return []
    with open(DEVICES_FILE) as f:
        return json.load(f)


def try_connect(device_id: str, ip: str, local_key: str, version: float) -> dict | None:
    """Attempt to connect and return DPS status."""
    from agshome.hub import AGSHomeHub

    hub = AGSHomeHub(
        device_id=device_id,
        ip_address=ip,
        local_key=local_key,
        version=version,
    )

    if hub.connect():
        status = hub.status()
        hub.disconnect()
        return status
    return None


def scan_network():
    """Run TinyTuya network scan to find devices."""
    import tinytuya

    print("\nScanning local network for Tuya devices...")
    print("This may take 20-30 seconds...\n")

    devices = tinytuya.deviceScan(verbose=True)

    if not devices:
        print("No Tuya devices found on the network.")
        print("Ensure your PC is on the same 2.4GHz Wi-Fi as the hub.")
        return []

    found = []
    for ip, info in devices.items():
        print(f"  Found: {ip} — ID: {info.get('gwId', '?')}, version: {info.get('version', '?')}")
        found.append({"ip": ip, **info})

    return found


def save_hub_config(device_id: str, ip: str, local_key: str, version: float):
    """Save hub connection details to config.json."""
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            config = json.load(f)

    config["hub"] = {
        "device_id": device_id,
        "ip_address": ip,
        "local_key": local_key,
        "protocol_version": version,
    }

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nHub config saved to {CONFIG_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Discover your AGSHome alarm hub and map its data points"
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan the local network for Tuya devices"
    )
    parser.add_argument(
        "--ip", type=str, default=None,
        help="Hub IP address (skip auto-detection)"
    )
    parser.add_argument(
        "--version", type=float, default=None,
        help="Protocol version to try (3.3, 3.4, or 3.5)"
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  AGSHome Hub Discovery Tool")
    print("=" * 60)

    # Step 1: Get device credentials
    devices = load_devices()
    if not devices:
        print(f"\n  No {DEVICES_FILE} found.")
        print("  Run the TinyTuya wizard first:")
        print("    python -m tinytuya wizard")

        if args.scan:
            print("\n  Running network scan instead...\n")
            scan_network()
        sys.exit(1)

    # Use first device (the alarm hub)
    device = devices[0]
    device_id = device["id"]
    local_key = device["key"]
    ip = args.ip or device.get("ip", "")

    print(f"\n  Device: {device.get('name', 'Unknown')}")
    print(f"  ID:     {device_id}")
    print(f"  Model:  {device.get('model', 'Unknown')}")

    # Step 2: Find the hub IP if not known
    if not ip:
        print("\n  No IP address in devices.json — scanning network...")
        if args.scan:
            found = scan_network()
            for d in found:
                if d.get("gwId") == device_id:
                    ip = d["ip"]
                    print(f"\n  Matched hub at {ip}")
                    break
        if not ip:
            print("\n  Could not find hub IP. Try:")
            print("    python discover.py --ip <HUB_IP>")
            print("    python discover.py --scan")
            sys.exit(1)

    print(f"  IP:     {ip}")

    # Step 3: Try connecting with different protocol versions
    versions_to_try = [args.version] if args.version else [3.3, 3.4, 3.5]

    status = None
    working_version = None

    for ver in versions_to_try:
        print(f"\n  Trying protocol version {ver}...")
        status = try_connect(device_id, ip, local_key, ver)
        if status and "error" not in status:
            working_version = ver
            break
        elif status:
            print(f"    Error: {status.get('error', 'Unknown')}")

    if not status or "error" in status:
        print("\n  Could not connect to hub with any protocol version.")
        print("  Possible issues:")
        print("  - Hub and PC not on the same network")
        print("  - Local key has changed (re-run tinytuya wizard)")
        print("  - Firewall blocking UDP 6666/6667 or TCP 6668")
        sys.exit(1)

    # Step 4: Display DPS map
    print(f"\n  Connected! (protocol v{working_version})")
    print(f"\n  {'DPS Index':<12} {'Value':<20} {'Description'}")
    print(f"  {'-' * 12} {'-' * 20} {'-' * 20}")

    from agshome.dps_map import describe_dps

    for idx in sorted(status.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        val = status[idx]
        desc = describe_dps(idx, val)
        print(f"  {idx:<12} {str(val):<20} {desc}")

    # Step 5: Save config
    save_hub_config(device_id, ip, local_key, working_version)

    print("\nDone! You can now run:")
    print("  python dashboard.py")
    print()


if __name__ == "__main__":
    main()
