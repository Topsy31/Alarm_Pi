# AGSHome — Raspberry Pi Setup Guide

A headless alarm server that runs 24/7 on a Raspberry Pi, replacing the Windows laptop dependency.

## What You Need

- Raspberry Pi (any model — Pi 1 B+ works fine for hub control + notifications without camera)
- **Note:** Pi 1 B+ uses 32-bit ARMv6 — select **Raspberry Pi OS Lite (32-bit)** in Imager
- MicroSD card (16GB+, Class 10)
- Power supply (official Pi PSU recommended)
- Network connection (wired Ethernet recommended for reliability, Wi-Fi works)
- Same local network as the AGSHome alarm hub

---

## Step 1 — Flash the Pi

1. Download **Raspberry Pi Imager** from [raspberrypi.com/software](https://www.raspberrypi.com/software/)
2. Choose **Raspberry Pi OS Lite (64-bit)** — no desktop needed
3. Before writing, click the gear icon (⚙) and configure:
   - **Hostname:** `agshome`
   - **Enable SSH:** yes, with password or public key
   - **Wi-Fi:** enter your network SSID and password (or use Ethernet)
   - **Username:** `pi`
4. Write to the SD card and boot the Pi

---

## Step 2 — Copy the Files

From your Windows machine, copy the `PiAlarm` folder to the Pi:

```bash
# On Windows, open a terminal in the Alarm folder and run:
scp -r PiAlarm/ pi@agshome.local:/home/pi/agshome
```

Or use **WinSCP** if you prefer a GUI.

---

## Step 3 — Run the Setup Script

SSH into the Pi and run the setup script:

```bash
ssh pi@agshome.local
cd /home/pi/agshome
bash setup.sh
```

The script will:
- Install system packages (Python, OpenCV dependencies, Avahi)
- Create a Python virtual environment with all dependencies
- Install and enable the `agshome` systemd service
- Create `config.json` from the template

---

## Step 4 — Configure the Hub

Edit `config.json` with your hub credentials:

```bash
nano /home/pi/agshome/config.json
```

Fill in the `hub` section with the same values from your Windows `config.json`:

```json
{
  "hub": {
    "device_id": "your_device_id",
    "ip_address": "192.168.0.52",
    "local_key": "your_local_key",
    "protocol_version": 3.4
  },
  "ntfy": {
    "enabled": true,
    "server": "https://ntfy.sh",
    "topic": "your-secret-topic",
    "priority_alert": 5,
    "priority_status": 2
  }
}
```

Save with `Ctrl+O`, exit with `Ctrl+X`.

---

## Step 5 — Start the Service

```bash
sudo systemctl start agshome
```

Check it's running:

```bash
sudo systemctl status agshome
```

You should see `active (running)`. View live logs:

```bash
journalctl -u agshome -f
```

---

## Step 6 — Test

From your phone or any browser on the network:

- **Mobile:** http://agshome.local:5000
- **Desktop:** http://agshome.local:5000/desktop

---

## Useful Commands

| Command | Purpose |
|---------|---------|
| `sudo systemctl start agshome` | Start the service |
| `sudo systemctl stop agshome` | Stop the service |
| `sudo systemctl restart agshome` | Restart after config changes |
| `sudo systemctl status agshome` | Check status |
| `journalctl -u agshome -f` | Live log output |
| `journalctl -u agshome --since "1 hour ago"` | Recent logs |
| `sudo systemctl disable agshome` | Disable auto-start |

---

## Updating

To update the code from your Windows machine:

```bash
# Copy updated files
scp -r PiAlarm/ pi@agshome.local:/home/pi/agshome

# Restart the service
ssh pi@agshome.local "sudo systemctl restart agshome"
```

---

## Troubleshooting

**Service won't start:**
```bash
journalctl -u agshome -n 50
```
Most common cause: wrong hub IP or key in `config.json`.

**agshome.local not resolving:**
Avahi should handle this automatically. Check it's running:
```bash
sudo systemctl status avahi-daemon
```

**Camera not connecting:**
Check the camera IP in `config.json` and confirm the camera is on the same network as the Pi.

**Pi not reachable after reboot:**
Give it 30–60 seconds — the network stack and mDNS take a moment to come up on boot.

---

## File Structure on the Pi

```
/home/pi/agshome/
├── pi_service.py        # Systemd entry point (replaces tray.py)
├── server.py            # Flask REST API (unchanged from Windows version)
├── camera.py            # Camera integration
├── discover_camera.py   # Camera discovery tool
├── config.json          # Your credentials (not in git)
├── config_template.json # Template for reference
├── requirements.txt     # Python dependencies (headless opencv)
├── agshome.service      # Systemd service definition
├── setup.sh             # One-shot setup script
├── agshome/             # AGSHome hub control package
│   ├── __init__.py
│   ├── hub.py
│   └── dps_map.py
├── templates/           # HTML templates (mobile + desktop)
├── static/              # Icons for phone home screen
└── venv/                # Python virtual environment (created by setup.sh)
```
