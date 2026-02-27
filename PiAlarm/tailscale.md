# Tailscale Remote Access Guide

Access the AGSHome alarm control page from anywhere in the world — no port forwarding,
no firewall changes, no dynamic DNS. Tailscale creates an encrypted VPN tunnel between
your devices over the internet.

---

## What Tailscale Does

Tailscale connects your devices into a private network (called a "tailnet") that works
over the internet. Once set up:

- Your phone can reach the Pi as if it were on your home Wi-Fi — from anywhere
- No open ports on your router
- End-to-end encrypted (WireGuard protocol)
- Free for personal use (up to 100 devices)

---

## What You Need

- A Tailscale account (free) — sign up at [tailscale.com](https://tailscale.com)
- Tailscale installed on the Pi
- Tailscale installed on your phone

---

## Step 1 — Create a Tailscale Account

1. Go to [tailscale.com](https://tailscale.com) and sign up (free)
2. You can sign in with Google, Microsoft, or GitHub — no separate password needed
3. Note your account email — you'll use it to authenticate each device

---

## Step 2 — Install Tailscale on the Pi

SSH into the Pi:
```bash
ssh pi@192.168.0.56
```

Install Tailscale:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

Start Tailscale and authenticate:
```bash
sudo tailscale up
```

This prints a URL — copy it and open it in a browser on any device. Log in with your
Tailscale account to authorise the Pi.

Confirm it's connected:
```bash
tailscale status
```

You should see the Pi listed with a Tailscale IP (e.g. `100.x.x.x`).

Get the Pi's Tailscale IP:
```bash
tailscale ip -4
```

Note this IP — you'll use it to access the alarm from outside your home network.

---

## Step 3 — Install Tailscale on Your Phone

- **iPhone:** App Store → search "Tailscale" → install
- **Android:** Play Store → search "Tailscale" → install

Open the app and sign in with the same Tailscale account.

The app will ask to set up a VPN profile — allow it.

---

## Step 4 — Test Remote Access

With Tailscale running on both devices, open your phone browser and go to:

```
http://100.x.x.x:5000
```

(Replace `100.x.x.x` with the Pi's Tailscale IP from Step 2.)

You should see the AGSHome alarm control page — even when your phone is on mobile data,
not your home Wi-Fi.

---

## Step 5 — Make It Easy to Access

Rather than remembering the Tailscale IP, use **MagicDNS** — Tailscale's built-in
hostname resolution.

Enable it in the Tailscale admin console:
1. Go to [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)
2. Enable **MagicDNS**

Then access the alarm page using the hostname:
```
http://agshome:5000
```

This works from any device on your tailnet, anywhere in the world.

---

## Step 6 — Auto-start Tailscale on the Pi

Tailscale should start automatically on boot. Confirm it's enabled:
```bash
sudo systemctl enable tailscaled
sudo systemctl status tailscaled
```

You should see `active (running)`. No further action needed — the Pi will reconnect
to Tailscale automatically after every reboot.

---

## Useful Commands

| Command | Purpose |
|---------|---------|
| `tailscale status` | Show connected devices |
| `tailscale ip -4` | Show this device's Tailscale IP |
| `sudo tailscale up` | Connect / re-authenticate |
| `sudo tailscale down` | Disconnect from Tailscale |
| `sudo systemctl status tailscaled` | Check Tailscale service |

---

## Tailscale Admin Console

Manage your devices at: [login.tailscale.com/admin](https://login.tailscale.com/admin)

Useful things you can do:
- See all connected devices and their Tailscale IPs
- Revoke access for a device (e.g. lost phone)
- Enable MagicDNS for hostname access
- Set up access controls (ACLs) if needed

---

## Security Notes

- Tailscale uses WireGuard — one of the most audited VPN protocols available
- Only devices logged into your Tailscale account can connect
- No ports are opened on your Sky router — traffic goes via Tailscale's relay servers
- If your phone is lost or stolen, revoke it immediately in the admin console
- The alarm page has no login — anyone on your tailnet can control it. Keep your
  Tailscale account secure (use 2FA on your Google/Microsoft/GitHub account)

---

## Troubleshooting

**Pi not appearing in Tailscale status:**
```bash
sudo tailscale up
```
Re-authenticate if the session has expired.

**Can't reach alarm page remotely:**
- Confirm Tailscale is running on both phone and Pi: `tailscale status`
- Make sure you're using the Tailscale IP (`100.x.x.x`), not the local IP (`192.168.0.56`)
- Check the alarm service is running: `sudo systemctl status agshome`

**MagicDNS hostname not resolving:**
- Confirm MagicDNS is enabled in the admin console
- Toggle Tailscale off and on again on your phone

---

## Notes

- Tailscale is free for personal use (up to 100 devices)
- The free plan is sufficient for home use — no subscription needed
- Tailscale does not route all your internet traffic through the VPN by default —
  only traffic destined for your tailnet devices goes through the tunnel
- The alarm service (port 5000) and Pi-hole dashboard (port 80) are both accessible
  via Tailscale once set up
