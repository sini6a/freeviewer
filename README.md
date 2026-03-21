# FreeViewer

A self-hosted remote desktop system. A lightweight web server handles authentication, device pairing, and signaling. A small agent runs on each remote machine and streams its screen over WebRTC. You connect from any browser — no plugins required.

```
Browser  ──►  Server (Flask + Socket.IO)  ◄──  Agent (Windows PC)
                        │
                   WebRTC signal
                        │
Browser  ◄──────── Video stream ────────►  Agent
```

---

## Requirements

| Component | Requirement |
|-----------|-------------|
| Server    | Python 3.11+ on any OS (Windows, Linux, macOS) |
| Agent     | Python 3.11+ **on Windows** (uses Win32 input APIs) |
| Browser   | Any modern browser with WebRTC support |

---

## Project Structure

```
freeviewer/
├── server/
│   ├── server.py          # Flask + Socket.IO server
│   ├── .env               # Server configuration (create from .env.example)
│   └── templates/         # Jinja2 HTML templates
│       ├── base.html
│       ├── login.html
│       ├── register.html
│       ├── dashboard.html
│       ├── pair.html
│       └── connect.html
└── agent/
    ├── agent.py           # Headless agent (console)
    ├── agent_gui.py       # GUI agent (tkinter, recommended)
    ├── build.py           # Build standalone .exe with PyInstaller
    └── .env               # Agent configuration (create from .env.example)
```

---

## Server Setup

### 1. Create a virtual environment

```bash
cd freeviewer
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / macOS
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install flask flask-socketio python-dotenv werkzeug eventlet
```

### 3. Create `server/.env`

```env
SECRET_KEY=replace-with-a-long-random-string
DATABASE_URL=
PORT=5000
DEBUG=false
PAIRING_CODE_LENGTH=6
PAIRING_EXPIRY_MINUTES=10
```

Generate a secure `SECRET_KEY`:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Leave `DATABASE_URL` blank — the server will create `freeviewer.db` next to `server.py` automatically.

### 4. Run the server

```bash
cd server
python server.py
```

Open `http://localhost:5000` in your browser.

### 5. Create the admin account

On first visit you will be prompted to create the only admin account. Registration is permanently disabled after the first account is created.

---

## Agent Setup

The agent runs on the Windows machine you want to control remotely.

### Option A — Run from source

#### 1. Install dependencies

```bash
cd agent
pip install python-dotenv aiortc opencv-python numpy mss pyautogui pynput python-socketio[asyncio_client] aiohttp
```

#### 2. Create `agent/.env`

```env
SERVER_URL=http://YOUR_SERVER_IP:5000
```

Replace `YOUR_SERVER_IP` with the IP or hostname of the machine running the server.

#### 3. Run the GUI agent (recommended)

```bash
python agent_gui.py
```

Or the headless console agent:

```bash
python agent.py
```

---

### Option B — Standalone `.exe` (no Python needed on remote machine)

#### 1. Install PyInstaller

```bash
pip install pyinstaller
```

#### 2. Edit `agent/.env` with the correct server URL before building

```env
SERVER_URL=http://YOUR_SERVER_IP:5000
```

#### 3. Build

```bash
cd agent
python build.py
```

The exe is output to `agent/dist/FreeViewer-Agent.exe`. Copy it to any Windows machine — no Python installation required.

> **Note:** The exe bundles the `.env` file at build time. If your server URL changes, rebuild the exe.

---

## Pairing a Device

Pairing links an agent to your account so only you can connect to it.

1. In the dashboard, click **Pair Device** → **Generate new code**
2. A 6-digit code is shown (valid for 10 minutes)
3. On the remote machine, run the agent — it will ask for the pairing code on first launch
4. Enter the code and press **Connect**
5. The device appears in your dashboard as **Paired & Online**

On subsequent runs the agent reconnects automatically using saved credentials (`agent_creds.json`). No code is needed again unless credentials are deleted or the device is removed from the dashboard.

---

## Connecting to a Remote Machine

1. Open the dashboard — online paired devices show a green **Connect** button
2. Click **Connect** — the browser opens a full-screen remote view
3. **Controls forwarded to the remote machine:**
   - Left / right / middle mouse button
   - Mouse movement and scroll wheel
   - Full keyboard: all keys, Ctrl, Alt, Shift, Win combinations, F1–F12, numpad

---

## UAC and Administrator Rights

The GUI agent (`agent_gui.py` / `FreeViewer-Agent.exe`) **requests administrator privileges automatically** on launch. This is required so the agent can send mouse and keyboard input to windows that are themselves running as Administrator (e.g. Task Manager, installers).

For full control of all windows, **User Account Control (UAC) should be disabled** on the remote machine. The agent will show a warning dialog on startup if UAC is detected and offer a button to disable it. A system restart is required after disabling UAC.

To disable UAC manually:
1. Open **Control Panel → User Accounts → Change User Account Control settings**
2. Drag the slider to **Never notify**
3. Click OK and restart

> Without disabling UAC, the agent can still control most applications but cannot interact with elevated (Administrator) windows.

---

## Device Management

| Action | How |
|--------|-----|
| Rename a device | Click the pencil icon on any device row |
| Delete a device | Click the trash icon — the agent is force-disconnected |
| Unblock a device | Click **Unblock** on a blocked device row |
| Search devices | Type in the search box in the Devices card header |

### Device states

| Status | Meaning |
|--------|---------|
| **Online** | Agent is connected to the server right now |
| **Offline** | Agent is not connected |
| **Paired** | Device is linked to your account |
| **Pending** | Device has not completed pairing yet |
| **Blocked** | Device presented an invalid token — blocked until manually unblocked |

---

## Reconnection Behaviour

The agent reconnects to the server automatically if the connection is lost (e.g. server restart, network drop). Reconnection uses exponential backoff starting at 2 seconds and capping at 60 seconds. No manual restart of the agent is needed.

---

## Internet / Remote Access

### Local network
Works out of the box. Point `SERVER_URL` at the server's LAN IP.

### Over the internet
The server must be reachable from the internet. Options:
- Run the server on a VPS (DigitalOcean, Hetzner, etc.) and use its public IP
- Port-forward port `5000` on your home router to the server machine
- Use a reverse proxy (nginx) with a domain name and HTTPS

The agent always makes **outbound** connections to the server — no port forwarding is needed on the remote machine side.

#### WebRTC and NAT

The video stream uses WebRTC. The current configuration uses a STUN server which works for most home and office routers (~80% of NAT types). For strict corporate networks, mobile carriers, or carrier-grade NAT, a **TURN server** is needed to relay the stream.

To add TURN support, update the ICE server list in both places:

**`server/templates/connect.html`** — inside `startSession()`:
```javascript
pc = new RTCPeerConnection({
    iceServers: [
        { urls: 'stun:stun.l.google.com:19302' },
        {
            urls: 'turn:your-turn-server:3478',
            username: 'username',
            credential: 'password'
        }
    ]
});
```

**`agent/agent_gui.py`** — inside `on_signal()`:
```python
pc = RTCPeerConnection(configuration={
    "iceServers": [
        {"urls": "stun:stun.l.google.com:19302"},
        {
            "urls": "turn:your-turn-server:3478",
            "username": "username",
            "credential": "password",
        },
    ]
})
```

Free TURN servers are available at [Metered](https://www.metered.ca/turn-server). Self-hosted: [coturn](https://github.com/coturn/coturn).

---

## Configuration Reference

### `server/.env`

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | — | Flask session secret. **Required.** Generate with `secrets.token_hex(32)` |
| `DATABASE_URL` | *(blank)* | SQLite path. Leave blank to use `freeviewer.db` next to `server.py` |
| `PORT` | `5000` | Port the server listens on |
| `DEBUG` | `false` | Enable Flask debug mode. Set `false` in production |
| `PAIRING_CODE_LENGTH` | `6` | Number of digits in a pairing code |
| `PAIRING_EXPIRY_MINUTES` | `10` | Minutes before an unused pairing code expires |

### `agent/.env`

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_URL` | `http://127.0.0.1:5000` | Full URL of the FreeViewer server |

---

## Security Notes

- The admin account is the only account — there is no multi-user support
- Device tokens are 256-bit random secrets stored in `agent_creds.json` on the remote machine
- If a device presents an invalid token it is immediately **blocked** and cannot re-pair without manual admin intervention from the dashboard
- All communication between the browser and server uses your existing session cookie — use HTTPS in production
- The `.env` files and `agent_creds.json` are excluded from git via `.gitignore`
