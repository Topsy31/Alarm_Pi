# CLAUDE.md - AGSHome + O-KAM Security Integration

## Project Overview

A Python toolkit for controlling an AGSHome alarm hub and O-KAM camera from a single Windows application on the local network.

## Tech Stack

- **Language:** Python 3.9+
- **Alarm:** TinyTuya (Tuya local API, encrypted)
- **Camera:** OpenCV (RTSP stream), Pillow (image conversion)
- **GUI:** tkinter
- **Platform:** Windows (local network only)

## Key Files

| File | Purpose |
|------|---------|
| `dashboard.py` | Unified GUI — camera feed + alarm controls |
| `camera.py` | O-KAM camera RTSP wrapper (connect, stream, snapshot, motion detection) |
| `discover_camera.py` | Network scanner to find camera and test RTSP URLs |
| `config_template.json` | Template config — copy to `config.json` and fill in keys |
| `requirements.txt` | Python dependencies |

## Build / Run Commands

```bash
pip install -r requirements.txt          # Install dependencies
python -m tinytuya wizard                # One-time: get Tuya device keys
python discover_camera.py                # Find camera on network
python dashboard.py                      # Launch GUI dashboard
python dashboard.py --headless           # Headless monitoring mode
```

## Conventions

- **British English** in all documentation and comments
- Config secrets (`config.json`, `devices.json`) are gitignored — never commit
- `config_template.json` is the safe-to-commit template
- Snapshots saved to `snapshots/` directory (gitignored)

## Architecture Notes

- All communication is local network only — no cloud dependency at runtime
- Camera stream uses RTSP over TCP (port 10555 for O-KAM, fallbacks to 554/8554)
- Alarm hub uses Tuya encrypted local protocol (UDP discovery + TCP control)
- Dashboard connects to both devices in background threads; GUI updates via `root.after()`
- The `agshome/` package (hub, camera, dps_map modules) is referenced by dashboard but not yet created as standalone files — currently embedded in `camera.py` and imported in `dashboard.py`

## Network Requirements

- UDP 6666, 6667, 7000 (Tuya discovery)
- TCP 6668 (Tuya control)
- TCP 10555 (O-KAM RTSP, may vary)
