# Pi-hole Setup Guide

Block ads and tracking for every device on your network using your Raspberry Pi.

---

## What Pi-hole Does

Pi-hole acts as a DNS server for your home network. When any device (phone, laptop, TV)
looks up a website, the request goes through the Pi first. If the domain is on a blocklist
(ads, trackers, telemetry), Pi-hole blocks it. No app installs needed on any device.

---

## Before You Start

- The alarm service must already be running (it will keep running throughout)
- You need SSH access to the Pi (`ssh pi@192.168.0.56`)
- The Pi needs internet access (`ping google.com -c 3` to confirm)

---

## Step 1 — Set a Static IP on the Pi ✓ Done

The Pi's IP is fixed at `192.168.0.56` via two mechanisms:

1. **DHCP reservation on the Sky router** — the router always assigns `.56` to the Pi's MAC address (`b8:27:eb:6b:ba:45`)
2. **Static IP in `/etc/dhcpcd.conf`** on the Pi itself

No further action needed here.

---

## Step 2 — Install Pi-hole ✓ Done

**Before running the installer, make sure:**
- The Ethernet cable is plugged into the Pi and the router
- You are SSHed in over Ethernet (not Wi-Fi)
- Confirm you are wired: `ip addr show eth0` — you should see an IP address listed

If Pi-hole was previously started and exited, clean up first:
```bash
pihole uninstall 2>/dev/null; sudo apt-get remove -y pihole 2>/dev/null; true
```
(Safe to run even if nothing was installed — errors can be ignored.)

Run the installer:
```bash
curl -sSL https://install.pi-hole.net | bash
```

This launches an interactive installer. Work through the screens:

| Screen | What to choose |
|--------|---------------|
| Welcome | OK |
| Donate | OK |
| Static IP | Keep current IP (192.168.0.56) — confirm Yes |
| Interface | `eth0` (wired) — **not wlan0** |
| Upstream DNS | Google (8.8.8.8) — or Cloudflare (1.1.1.1) if preferred |
| Blocklists | Keep defaults — Yes |
| Admin web interface | Yes |
| Web server (lighttpd) | Yes |
| Log queries | Yes |
| Privacy mode | Show everything (option 0) |

At the end it will show you a **web interface password** — write this down.
If you missed it, reset it with: `pihole -a -p`

**If you accidentally selected wlan0**, fix it after installation with:
```bash
pihole -r
```
Select `Reconfigure` and choose `eth0` this time.

---

## Step 3 — Confirm Pi-hole is Running ✓ Done

```bash
pihole status
```

You should see:
```
  [✓] FTL is listening on port 53
  [✓] Pi-hole blocking is enabled
```

Also confirm the alarm service is still running:
```bash
sudo systemctl status agshome
```

---

## Step 4 — Network-wide DNS via Pi-hole DHCP ✓ Done

### Why we used this approach

The Sky router does not expose DNS server settings — you cannot tell it to use Pi-hole
as the DNS for the whole network. The workaround is to use **Pi-hole's built-in DHCP server**
instead of the router's. Pi-hole then hands out IP addresses to all devices and automatically
tells each one to use `192.168.0.56` as its DNS server.

### What was configured

**Pi-hole DHCP** (Admin dashboard → Settings → DHCP):

| Setting | Value |
|---------|-------|
| DHCP server enabled | Yes |
| Start | `192.168.0.2` |
| End | `192.168.0.254` |
| Router (gateway) | `192.168.0.1` |
| Netmask | (automatic) |

**Sky router** (LAN TCP/IP Setup):
- Unchecked **"Use Router as DHCP Server"**
- Applied

Every device on the network now gets its DNS set to `192.168.0.56` automatically
when it connects — no per-device configuration needed.

### Resilience — Sky Router DHCP Re-enabled as Safety Net ✓ Done

The network includes locked-down devices (Sky Stream puck) where DNS cannot be set
manually. If Pi-hole goes down, these devices lose DNS and streaming stops.

**Solution:** Sky router DHCP was re-enabled alongside Pi-hole DHCP.

- Pi-hole wins the DHCP race in normal operation (faster, wired)
- Sky router sits idle but ready — if Pi goes down, it picks up new DHCP requests
- Devices that renew their lease while Pi is down get Sky's DNS and streaming continues
- Two DHCP servers on the same subnet is technically non-standard but works reliably
  on a home network with a single subnet

**DNS fallback per device** (phone + PC — set manually):
- Primary DNS: `192.168.0.56` (Pi-hole)
- Secondary DNS: `8.8.8.8` (Google — used automatically if Pi-hole doesn't respond)

### Final Configuration Summary

| Layer | Primary | Fallback |
|-------|---------|----------|
| DHCP | Pi-hole | Sky router (re-enabled) |
| DNS | Pi-hole (`192.168.0.56`) | Google `8.8.8.8` (manual on phone + PC) |
| Ad blocking | All devices via Pi-hole | None when Pi is down (acceptable) |
| Sky Stream | Pi-hole DNS | Sky router DNS if Pi fails |

---

## Step 5 — Test It's Working

From any device on your network, open a browser and go to:

```
http://192.168.0.56/admin
```

You should see the Pi-hole dashboard showing DNS queries being processed.

To test blocking, visit a page you know has ads — they should be gone.

---

## Pi-hole Admin Dashboard

Access at: `http://192.168.0.56/admin`

Useful things you can do:
- See a live graph of DNS queries and blocks
- Add/remove domains from the blocklist
- Temporarily disable blocking (useful for troubleshooting)
- See which devices are making the most requests
- Add a custom blocklist URL

---

## Useful Commands

| Command | Purpose |
|---------|---------|
| `pihole status` | Check if Pi-hole is running |
| `pihole enable` | Enable blocking |
| `pihole disable` | Disable blocking (all traffic passes through) |
| `pihole disable 5m` | Disable for 5 minutes then re-enable |
| `pihole -up` | Update Pi-hole to latest version |
| `pihole -g` | Update the blocklists |
| `pihole tail` | Watch live DNS queries |
| `pihole restartdns` | Restart the DNS service |

---

## Reverting to Normal (Sky Router DHCP)

Two steps to undo everything:

1. **Sky router** → LAN TCP/IP Setup → check **"Use Router as DHCP Server"** → Apply
2. **Pi-hole dashboard** → Settings → DHCP → uncheck **"DHCP server enabled"** → Save

The router takes over within seconds. Devices renew their leases automatically.

**Emergency revert** — if you lose network access entirely, just **power off the Pi**.
The Sky router will automatically take over DHCP once the Pi disappears.
Everything returns to normal within a minute.

---

## Troubleshooting

**Internet stops working after setup:**
- Pi-hole may have crashed. SSH in and run `pihole restartdns`
- Or power off the Pi — the Sky router will take over DHCP automatically

**A website is blocked that shouldn't be:**
- Go to the admin dashboard → Whitelist → add the domain

**Pi-hole dashboard not loading:**
- Pi-hole uses port 80. Check it's running: `pihole status`

**Devices not getting Pi-hole DNS:**
- Pi-hole DHCP should be enabled — check at Settings → DHCP
- Reconnect the device to Wi-Fi to get a fresh DHCP lease
- Sky router DHCP is also enabled as a fallback — this is intentional

**Alarm service stopped working:**
- The alarm uses port 5000, Pi-hole uses port 80 — they don't conflict
- Check: `sudo systemctl status agshome`

---

## Keeping Pi-hole Up to Date

Run this occasionally (monthly is fine):
```bash
pihole -up
```

This updates Pi-hole and refreshes the blocklists.

---

## Notes

- Pi-hole runs as its own service and does not interfere with the alarm
- If the Pi is powered off, devices fall back to the Sky router's DHCP (and Sky's DNS) automatically
- The admin dashboard password was shown at the end of installation — if you lost it,
  reset it with: `pihole -a -p`
- Pi MAC address: `b8:27:eb:6b:ba:45` (reserved to `192.168.0.56` in Sky router)
