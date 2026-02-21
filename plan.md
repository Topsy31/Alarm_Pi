# Alarm Project — Research & Implementation Plan

## Project Summary

A Python toolkit to control an **AGSHome alarm hub** (Tuya local protocol) and **O-KAM camera** (RTSP stream) from a single Windows application on the local network. Currently: scaffold code exists but the `agshome/` package it imports doesn't exist yet, so nothing runs.

---

## Current State

### What exists
| File | Status | Notes |
|------|--------|-------|
| `dashboard.py` | Written, **not runnable** | Imports `agshome.hub`, `agshome.dps_map`, `agshome.camera` — none of which exist as files |
| `camera.py` | Written, **standalone module** | Full OKamCamera class + RTSP probing. Lives at root, but dashboard imports from `agshome.camera` |
| `discover_camera.py` | Written, **not runnable** | Imports `from agshome.camera import probe_camera_rtsp` |
| `config_template.json` | Complete | Hub + camera + cloud + monitoring config |
| `requirements.txt` | Complete | tinytuya, colorama, opencv-python, numpy, Pillow |
| `README.md` | Written | References scripts that don't exist yet (`discover.py`, `monitor.py`, `control.py`) |
| `CLAUDE.md` | Written | Accurate architecture notes |
| `.gitignore` | Complete | Covers secrets, snapshots, logs |

### What's missing
1. **`agshome/` package** — the entire package directory:
   - `agshome/__init__.py`
   - `agshome/hub.py` — AGSHomeHub class (TinyTuya wrapper for the alarm)
   - `agshome/dps_map.py` — AlarmMode enum + DPS index mapping
   - `agshome/camera.py` — should be the current `camera.py` moved here
2. **`discover.py`** — alarm hub network discovery + DPS mapping
3. **`monitor.py`** — headless alarm event monitor (referenced in README)
4. **`control.py`** — CLI arm/disarm tool (referenced in README)

### Key blocker
The dashboard imports `from agshome.hub import AGSHomeHub` etc. — until the `agshome/` package exists, **nothing in the project can run**.

---

## Research Findings

### AGSHome Alarm Hub (Tuya Protocol)
- AGSHome is a Tuya-based security brand — uses encrypted local protocol
- TinyTuya communicates locally via UDP discovery (ports 6666/6667/7000) and TCP control (port 6668)
- Protocol versions: 3.3 is most common; 3.4/3.5 for newer devices
- **DPS (Data Point Schema)**: each device function has a numeric index (e.g. DPS 2 = alarm mode, DPS 104 = siren)
- Hub keys are obtained via `python -m tinytuya wizard` using Tuya IoT cloud credentials
- TinyTuya does **not** support real-time push — requires polling (`device.status()`)
- Common hub DPS indices for alarm panels:
  - `1` — master switch or zone trigger
  - `2` — alarm mode (home/away/disarmed)
  - `104` — siren on/off
  - Various higher indices — individual sensor zones

### O-KAM Camera (RTSP)
- **Ports**: 10554 (community-reported) AND 10555 (official docs) — both should be tried
- **Path case sensitivity**: both `TCP/av0_0` and `tcp/av0_0` should be probed
- **Default credentials**: `admin` / `888888` (not blank as current code assumes)
- **Security warning**: Vstarcam cameras are actively targeted by botnets (Eleven11bot). Must NEVER be exposed to the internet. Recommend VLAN isolation.
- **Web interface**: `http://<camera-ip>:9999/monitor2.htm` for direct access
- **ONVIF**: supported on port 10080 (non-standard)
- **OpenCV read() hang**: known issue — `cap.read()` can hang permanently after connection drop. Need thread-based timeout wrapper.
- Current approach (OpenCV + FFmpeg) is reasonable for this project

---

## Implementation Plan

### Phase 1: Create the `agshome/` package (unblocks everything)

**1.1 — `agshome/__init__.py`**
- Package init, version string, convenience imports

**1.2 — `agshome/dps_map.py`**
- `AlarmMode` enum: AWAY, HOME, DISARMED (+ SOS if supported)
- DPS index constants for the AGSHome hub
- Helper to decode raw DPS values into human-readable names

**1.3 — `agshome/hub.py`**
- `AGSHomeHub` class wrapping TinyTuya
- Methods: `connect()`, `disconnect()`, `status()`, `set_mode()`, `trigger_siren()`, `poll_loop()`
- Listener/callback system for DPS changes (`add_listener()`)
- Reconnection logic with configurable retry

**1.4 — `agshome/camera.py`**
- Move current root `camera.py` into `agshome/camera.py`
- Delete root `camera.py`
- No functional changes needed — it's already well-written

### Phase 2: Create the missing CLI scripts

**2.1 — `discover.py`**
- Scan local network for Tuya devices (TinyTuya scanner)
- Display found devices with ID, IP, version
- Map DPS indices by querying device status
- Save to `config.json`

**2.2 — `control.py`**
- CLI tool: `python control.py arm-away`, `python control.py disarm`, etc.
- Reads config.json, connects to hub, executes command, exits

**2.3 — `monitor.py`**
- Headless alarm polling loop with logging
- Writes events to log file
- Optional webhook/notification support

### Phase 3: Fix issues found during research

**3.1 — Camera discovery improvements**
- Add port 10554 to `RTSP_PORTS` and `RTSP_URL_PATTERNS`
- Add lowercase path variants (`tcp/av0_0`)
- Default credentials to `admin` / `888888` in config template
- Add authenticated URL variants to probe list

**3.2 — OpenCV robustness**
- Add thread-based timeout wrapper around `cap.read()` to prevent permanent hangs
- Improve reconnection logic in `_stream_loop`

**3.3 — Security documentation**
- Add prominent warning in README about never exposing camera to internet
- Document VLAN isolation recommendation
- Note default credential risk

### Phase 4: Testing & polish

**4.1 — Verify imports and module structure**
- Ensure `dashboard.py`, `discover_camera.py` can import from `agshome/` correctly
- Test with `python -c "from agshome.hub import AGSHomeHub"` etc.

**4.2 — Config validation**
- Add startup check that `config.json` exists and has valid values
- Clear error messages for missing keys

**4.3 — Initial git commit**
- Stage all files, commit with descriptive message

---

## Priority Order

1. **Phase 1** (critical — nothing works without it)
2. **Phase 3.1** (camera fixes — quick wins from research)
3. **Phase 2** (CLI scripts — fills out the README's promised features)
4. **Phase 3.2–3.3** (robustness + docs)
5. **Phase 4** (polish)

---

## Open Questions (need your input)

1. **Do you have the actual AGSHome hub?** If so, can you run `python -m tinytuya wizard` and share the DPS map? The DPS indices vary by device model — I'll use common alarm panel indices as defaults, but real data would be more accurate.
2. **Protocol version**: Is your hub on 3.3, 3.4, or 3.5? (Config template says 3.3)
3. **Camera credentials**: Have you changed from the default `admin`/`888888`?
4. **Which O-KAM model** do you have? (affects port and path specifics)
5. **Do you want the CLI scripts** (`discover.py`, `monitor.py`, `control.py`) or is the dashboard sufficient for now?
