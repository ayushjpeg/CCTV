## CCTV WebRTC Streaming System

A self-hosted CCTV platform built with Flask, Flask-SocketIO, and WebRTC.  
It lets one device broadcast a live camera feed while other devices connect over the network (or internet) with TURN/STUN assistance for NAT traversal. The app prioritizes low-latency, browser-based viewing and includes diagnostics for tricky relay scenarios such as laptops behind strict firewalls.

### Features
- WebRTC peer-to-peer streaming with VP8/Opus codecs.
- Flask + Socket.IO signaling server (`/socket.io`).
- TURN-first ICE policy with automatic bitrate tuning for constrained uplinks.
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

The app is currently hard-coded to prefer TURN (relay) candidates so even restrictive networks can broadcast. Update the ICE config in `templates/index.html` if you need multiple TURN servers or fallback STUN entries.

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

## 8. Contributing / next steps

- Add authentication/authorization for camera access.
- Introduce recording or snapshot APIs.
- Expand diagnostics page to test relay performance from the browser UI.

Issues and PRs are welcome! Document your environment (browser, OS, ISP) when reporting WebRTC bugs so we can reproduce NAT/firewall quirks.
