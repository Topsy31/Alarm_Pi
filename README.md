# AGSHome + O-KAM - Unified Security Integration

A Python toolkit for controlling your AGSHome alarm hub and O-KAM camera
from a single Windows application on your local network.

## Supported Devices

| Device | Protocol | Library |
|--------|----------|---------|
| AGSHome Security Hub | Tuya local API (encrypted) | TinyTuya |
| O-KAM Pro Camera | RTSP video stream | OpenCV |

## Prerequisites

1. **AGSHome hub** paired via the Smart Life app
2. **O-KAM camera** set up via the O-KAM Pro app
3. **Tuya IoT Developer account** (free) at https://iot.tuya.com
4. **Python 3.9+** on Windows
5. Your PC on the **same 2.4GHz Wi-Fi network** as both devices

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Get your alarm hub keys (one-time)
python -m tinytuya wizard

# 3. Discover your alarm hub
python discover.py

# 4. Discover your camera
python discover_camera.py

# 5. Launch the unified dashboard
python dashboard.py
```

## Getting Your Device Keys (One-Time Setup)

### AGSHome Hub (Tuya)
1. Go to https://iot.tuya.com and create a free developer account
2. Create a Cloud Project (select "Smart Home" and your region)
3. Subscribe to these APIs: IoT Core, Authorization, Smart Home Basic Service
4. Go to Devices > Link App Account > scan the QR code with Smart Life app
5. Run `python -m tinytuya wizard` and enter your API ID, Secret, and Region

### O-KAM Camera
1. Open the O-KAM Pro app and note the camera's IP address
2. Run `python discover_camera.py --ip <CAMERA_IP>` to find the RTSP stream
3. The tool will auto-save working settings to config.json

## Scripts

| Script | Purpose |
|--------|---------|
| `discover.py` | Find alarm hub and map its data points |
| `discover_camera.py` | Find camera and test RTSP stream |
| `dashboard.py` | Unified GUI with camera feed + alarm controls |
| `monitor.py` | Headless alarm event monitoring |
| `control.py` | Command-line alarm arm/disarm |

## Dashboard Features

- **Live camera feed** with timestamp overlay
- **Alarm status** panel with arm/disarm buttons
- **Auto-snapshot**: camera captures a frame when the alarm triggers
- **Event log** with timestamped sensor changes
- **Headless mode**: `python dashboard.py --headless` for server/background use

## Network Requirements

Your firewall must allow:
- UDP ports 6666, 6667, 7000 (Tuya device discovery)
- TCP port 6668 (Tuya device control)
- TCP port 10555 (O-KAM RTSP stream - may vary)

## Project Structure

```
agshome-integration/
├── config_template.json   # Template - copy to config.json
├── requirements.txt       # Python dependencies
├── discover.py           # Hub: scan network & map data points
├── discover_camera.py    # Camera: find RTSP stream
├── dashboard.py          # Unified GUI dashboard
├── monitor.py            # Hub: real-time event monitoring
├── control.py            # Hub: command-line arm/disarm
└── agshome/
    ├── __init__.py
    ├── hub.py            # AGSHome hub wrapper (Tuya protocol)
    ├── camera.py         # O-KAM camera wrapper (RTSP/OpenCV)
    └── dps_map.py        # Data point mapping & definitions
```

## Troubleshooting

### Alarm Hub
- **Device not found**: Ensure PC and hub are on same 2.4GHz network
- **Connection refused**: Check Windows Firewall for required ports
- **Decrypt errors**: Local Key may have changed - re-run the wizard
- **Empty status**: Try protocol version 3.4 or 3.5 in config

### Camera
- **No RTSP stream**: Some O-KAM firmware blocks local RTSP - check for updates
- **Black/no frames**: Try the sub stream (av0_1) instead of main (av0_0)
- **Wrong port**: Run discover_camera.py which tries ports 554, 8554, and 10555
- **Needs credentials**: Some models need username/password - check O-KAM app settings
