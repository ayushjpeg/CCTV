## CCTV WebRTC Streaming System

A self-hosted CCTV platform built with Flask, Flask-SocketIO, and WebRTC.  
It lets one device broadcast a live camera feed while other devices connect over the network (or internet) with TURN/STUN assistance for NAT traversal. The app prioritizes low-latency, browser-based viewing and includes diagnostics for tricky relay scenarios such as laptops behind strict firewalls.

### Features
- WebRTC peer-to-peer streaming with VP8/Opus codecs.
- Flask + Socket.IO signaling server (`/socket.io`).
- TURN-first ICE policy with automatic bitrate tuning for constrained uplinks.
- Live viewer counts with automatic broadcast health monitoring.
- Built-in call hub for on-demand or auto-pickup multi-party video calls between operators.
- Responsive UI with Broadcast/Watch tabs (`templates/index.html`).
- Dockerfile for container deployments plus bare-metal instructions.

---

## 1. Project structure

```
CCTV/
├─ app.py                 # Flask + Socket.IO signaling backend
├─ feed.py / streams.py   # Helpers for video feeds (optional experiments)
├─ templates/             # Broadcast/Watch UI pages (index.html)
├─ static/                # Shared CSS/assets
├─ requirements.txt       # Python dependencies
├─ Dockerfile             # Production container image
└─ readme.md              # You are here
```

---

## 2. Prerequisites

- Python 3.11+ (project tested on Python 3.11 / 3.13).
- Node/npm **not** required; all frontend assets are vanilla HTML/JS.
- A TURN server reachable from broadcasters & viewers.
	- Example config used in development: `turn:122.169.4.176:3478` with user `cctv` / password `wheresrusty`.
- Optional: Docker & docker-compose for containerized deployment.

---

## 3. Local setup (development)

```powershell
cd CCTV
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

The app defaults to `http://127.0.0.1:5000`.  Visit that URL from:
- A **broadcaster** device (laptop, phone) via the Broadcast tab.
- One or more **viewer** devices via the Watch tab.

> **Tip:** For remote devices, expose port 5000 behind a reverse proxy (nginx, Caddy, etc.) or run the Docker container behind HTTPS.

---

## 4. Docker build & run

```powershell
cd CCTV
docker build -t cctv-webrtc .
docker run --rm -p 5000:5000 --name cctv cctv-webrtc
```

Mount TLS certs and configure a proxy (nginx/Traefik) for HTTPS in production to prevent browsers from blocking camera/mic access.

---

## 5. TURN / STUN configuration

The app is currently hard-coded to prefer TURN (relay) candidates so even restrictive networks can broadcast. Update the ICE config in `templates/index.html` if you need multiple TURN servers or fallback STUN entries. Keep the new viewer-count heartbeat enabled so the UI can recover if a relay silently drops overnight.

Example Coturn snippet (used in testing):

```
listening-port=3478
tls-listening-port=5349
listening-ip=0.0.0.0
relay-ip=<public-ip>
external-ip=<public-ip>/<internal-ip>
realm=cctv.local
server-name=cctv-turn
fingerprint
lt-cred-mech
user=cctv:wheresrusty
no-tcp-relay=0
no-tlsv1
no-tlsv1_1
allowed-peer-ip=0.0.0.0-255.255.255.255
```

Forward UDP/TCP 3478 and the relay range (default `49152-65535`) on your router to the TURN host.

> **Tip:** Point your TURN DNS record (e.g., `turn.example.com`) at the router’s WAN IP using dynamic DNS. The frontend now references the TURN server via hostname (`TURN_HOST` in `templates/index.html`), so as long as DNS follows your WAN IP and the cron script keeps `external-ip` fresh, the clients always reach the correct relay endpoint.

---

## 6. Windows laptop firewall checklist

When broadcasting from Windows laptops, the firewall must allow **both outbound and inbound** TURN traffic:

1. Open **Windows Defender Firewall with Advanced Security**.
2. Create **Outbound** rules for UDP **and** TCP on ports `3478` and `49152-65535`.
3. Create matching **Inbound** rules for the same ports (UDP + TCP). UDP is required because TURN relays send media back to the broadcaster on high ports.
4. Add a **Program** rule allowing your browser (`chrome.exe`, `msedge.exe`, etc.) for inbound & outbound traffic.
5. Ensure the rules apply to the active profile (Public/Private/Domain).
6. Re-enable the firewall and verify streaming; it should now work just like when the firewall was disabled.

Optional verification:
- Run `Test-NetConnection 122.169.4.176 -Port 3478` to confirm connectivity.
- Use <https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/> with your TURN creds to confirm relay candidates appear.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Viewers connect to phone broadcaster but not laptop | Laptop firewall blocking TURN return path | Follow section 6 to add inbound rules |
| ICE state fails with policy `relay` | TURN not reachable from client | Test ports, verify DNS, ensure router forwards 3478 + relay range |
| Phone works on Wi-Fi but not LTE | Mobile carrier blocks UDP 3478 | Add `turn:server?transport=tcp` entries or host TURN on 80/443 |
| No cameras listed in Watch tab | Broadcaster not registered or Socket.IO path blocked | Ensure `/socket.io` accessible; check server logs |

Enable Chrome’s `chrome://webrtc-internals/` or Edge equivalent to inspect candidate pairs, bitrate, and connection states when debugging.

---

## 8. Auto-updating TURN external IP

Home ISPs often rotate your public IPv4 whenever the router reboots. Coturn needs that exact number in `external-ip=<wan>/<lan>` or peers will receive dead relay candidates. Use the helper script `scripts/update_turn_external_ip.py` on the TURN host to keep the config synced automatically:

```bash
python scripts/update_turn_external_ip.py \
	--config /etc/turnserver.conf \
	--internal-ip 192.168.1.2 \
	--restart-command "systemctl restart coturn"
```

- The script fetches the WAN IP (via https://api.ipify.org), updates the `external-ip` line if it changed, and runs the optional restart command.
- Schedule it via cron (`*/5 * * * * /usr/bin/python3 /opt/cctv/scripts/update_turn_external_ip.py …`) or a systemd timer to minimize downtime.
- If your TURN server lives on a VPS with a static IP, skip this script.

### Keep Cloudflare DNS synced too

If you expose TURN via a hostname such as `turn.ayux.in`, keep its A record updated alongside coturn by using `scripts/update_cloudflare_dns.py`:

```bash
python scripts/update_cloudflare_dns.py \
	--api-token $CF_API_TOKEN \
	--zone-id <zone_id> \
	--record-name turn.ayux.in \
	--ttl 120 \
	--proxied false
```

Steps:
1. Generate a Cloudflare API token with **Zone.DNS Edit** permission for your domain.
2. Find the zone ID on the Cloudflare dashboard (Overview → API → Zone ID).
3. Ensure the DNS record exists and is set to “DNS only” (grey cloud).
4. Schedule the script together with the TURN updater (e.g., run both inside the same cron job) so the hostname always points at the latest WAN IP.

---

## 10. Operator call hub & viewer analytics

The refreshed UI adds a **Call Hub** tab that lets camera operators see who is online, place calls, and auto-answer trusted parties:

1. Open the **Call Hub** tab, pick a display name, and toggle “Auto pickup” if this station should auto-answer.
2. Click **Go Online** so the backend registers your presence. Everyone online appears with status chips (“Auto pickup”, “Manual pickup”, “In call”).
3. Press **Call** next to a person to ring them. When they accept, both sides (plus any existing participants) enter a mesh call—if a third user dials while two people are speaking, they join the same call automatically, creating an instant conference.
4. Enable **Auto pickup** on unattended kiosks so critical alerts become drop-in video calls without human interaction.

The Broadcast tab now shows a **Live viewers** counter and keeps relays healthy overnight by softly retrying failed viewer sessions. Camera cards on the Watch tab display “N watching” badges so you can gauge demand at a glance.

---

## 9. Contributing / next steps

- Add authentication/authorization for camera access.
- Introduce recording or snapshot APIs.
- Expand diagnostics page to test relay performance from the browser UI.

Issues and PRs are welcome! Document your environment (browser, OS, ISP) when reporting WebRTC bugs so we can reproduce NAT/firewall quirks.

# This app is under dev
