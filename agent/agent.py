import asyncio
import threading
import time
import warnings
# aiohttp emits a ResourceWarning for its internal session during asyncio shutdown;
# the session is closed correctly — this is a false alarm from the GC running after the loop stops.
warnings.filterwarnings("ignore", message="Unclosed client session")
warnings.filterwarnings("ignore", message="Unclosed connector")
import cv2
import numpy as np
from mss import mss
import pyautogui
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import VideoFrame
import socketio
from pynput.keyboard import Controller, Key
import json
import os
import socket
import sys
import ctypes
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")


def _patch_sdp_bitrate(sdp: str, kbps: int) -> str:
    """Insert b=AS:<kbps> after the c= line in every video m= section."""
    sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(sep)
    result, in_video = [], False
    for line in lines:
        if line.startswith("m=video"):
            in_video = True
        elif line.startswith("m="):
            in_video = False
        result.append(line)
        if in_video and line.startswith("c="):
            result.append(f"b=AS:{kbps}")
    return sep.join(result)


def _build_rtc_config(ice_servers: list):
    """Build RTCConfiguration from a list of ICE server dicts, with graceful fallback."""
    if not ice_servers:
        return None
    try:
        from aiortc import RTCConfiguration, RTCIceServer
        servers = []
        for s in ice_servers:
            urls = s.get("urls", "")
            servers.append(RTCIceServer(
                urls=urls if isinstance(urls, list) else [urls],
                username=s.get("username"),
                credential=s.get("credential"),
            ))
        return RTCConfiguration(iceServers=servers)
    except Exception:
        return None

# Fix for Windows Scaling (High DPI) - ensures clicks hit the right pixel
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

keyboard = Controller()
screen_width, screen_height = pyautogui.size()

MOUSE_BUTTONS = {0: 'left', 1: 'middle', 2: 'right'}

CODE_TO_KEY = {
    **{f'Key{c}': c.lower() for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'},
    **{f'Digit{n}': n for n in '0123456789'},
    **{f'Numpad{n}': n for n in '0123456789'},
    **{f'F{n}': getattr(Key, f'f{n}') for n in range(1, 13)},
    'Space': Key.space,         'Enter': Key.enter,         'NumpadEnter': Key.enter,
    'Backspace': Key.backspace, 'Tab': Key.tab,             'Escape': Key.esc,
    'Delete': Key.delete,       'Insert': Key.insert,       'CapsLock': Key.caps_lock,
    'Home': Key.home,           'End': Key.end,
    'PageUp': Key.page_up,      'PageDown': Key.page_down,
    'ArrowUp': Key.up,          'ArrowDown': Key.down,
    'ArrowLeft': Key.left,      'ArrowRight': Key.right,
    'ShiftLeft': Key.shift_l,   'ShiftRight': Key.shift_r,
    'ControlLeft': Key.ctrl_l,  'ControlRight': Key.ctrl_r,
    'AltLeft': Key.alt_l,       'AltRight': Key.alt_r,
    'MetaLeft': Key.cmd,        'MetaRight': Key.cmd,
    'Minus': '-',      'Equal': '=',       'BracketLeft': '[',  'BracketRight': ']',
    'Backslash': '\\', 'Semicolon': ';',   'Quote': "'",        'Backquote': '`',
    'Comma': ',',      'Period': '.',       'Slash': '/',
    'NumpadAdd': '+',  'NumpadSubtract': '-', 'NumpadMultiply': '*',
    'NumpadDivide': '/', 'NumpadDecimal': '.',
}


def handle_input(msg):
    t = msg.get('type')
    try:
        if t in ('mousedown', 'mouseup'):
            button = MOUSE_BUTTONS.get(msg.get('button', 0), 'left')
            x = int(msg['x'] * screen_width)
            y = int(msg['y'] * screen_height)
            if t == 'mousedown':
                pyautogui.mouseDown(x, y, button=button)
            else:
                pyautogui.mouseUp(x, y, button=button)
        elif t == 'scroll':
            x = int(msg['x'] * screen_width)
            y = int(msg['y'] * screen_height)
            dy = msg.get('deltaY', 0)
            if dy:
                pyautogui.scroll(-1 if dy > 0 else 1, x, y)
        elif t == 'mousemove':
            x = int(msg['x'] * screen_width)
            y = int(msg['y'] * screen_height)
            pyautogui.moveTo(x, y)
        elif t in ('keydown', 'keyup'):
            pkey = CODE_TO_KEY.get(msg.get('code', ''))
            if pkey is not None:
                if t == 'keydown':
                    keyboard.press(pkey)
                else:
                    keyboard.release(pkey)
    except Exception as e:
        print(f"Input error: {e}")
device_name = socket.gethostname()
device_id = f"dev_{device_name.lower()}"

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:5000")
CREDS_FILE = Path(__file__).parent / "agent_creds.json"

sio = socketio.AsyncClient(
    reconnection=True,
    reconnection_attempts=0,       # unlimited
    reconnection_delay=2,
    reconnection_delay_max=60,
)
pc = None
_payload = None
_registration_failed = False
_force_disconnected = False


def load_creds():
    if CREDS_FILE.exists():
        try:
            return json.loads(CREDS_FILE.read_text())
        except Exception:
            pass
    return None


def save_creds(token):
    CREDS_FILE.write_text(json.dumps({"device_id": device_id, "device_token": token}))

MAX_WIDTH  = 1920
MAX_HEIGHT = 1080
TARGET_FPS = 20


class ScreenShareTrack(VideoStreamTrack):
    """Captures the screen in a background thread so recv() never blocks asyncio."""

    STATS_INTERVAL = 10.0

    def __init__(self):
        super().__init__()
        self._framerate    = TARGET_FPS
        self._latest       = None
        self._lock         = threading.Lock()
        self._running      = True
        self._recv_count   = 0
        self._recv_wait_ms = 0.0
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def stop(self):
        self._running = False
        super().stop()

    def _capture_loop(self):
        sct = mss()
        monitor   = sct.monitors[1]
        interval  = 1.0 / self._framerate
        cap_count = cap_ms = over_count = 0
        out_w = out_h = 0
        stats_t   = time.perf_counter()

        while self._running:
            t0  = time.perf_counter()
            img = np.array(sct.grab(monitor))
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            h, w = img.shape[:2]
            scale = min(MAX_WIDTH / w, MAX_HEIGHT / h, 1.0)
            if scale < 1.0:
                img = cv2.resize(img, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_LINEAR)
            # Cursor is drawn as a browser overlay — not burned into the frame
            with self._lock:
                self._latest = img

            elapsed    = time.perf_counter() - t0
            cap_ms    += elapsed * 1000
            cap_count += 1
            out_h, out_w = img.shape[:2]
            if elapsed > interval:
                over_count += 1

            now = time.perf_counter()
            if now - stats_t >= self.STATS_INTERVAL and cap_count:
                actual_fps = cap_count / (now - stats_t)
                avg_ms     = cap_ms / cap_count
                recv_avg   = (self._recv_wait_ms / self._recv_count
                              if self._recv_count else 0)
                print(
                    f"[capture] {actual_fps:.1f} fps  "
                    f"grab {avg_ms:.0f} ms/frame  "
                    f"out {out_w}×{out_h}  "
                    f"overruns {over_count}  "
                    f"recv wait {recv_avg:.0f} ms"
                )
                cap_count = cap_ms = over_count = 0
                self._recv_count = self._recv_wait_ms = 0
                stats_t = now

            time.sleep(max(interval - elapsed, 0.005))

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        t0 = time.perf_counter()
        while True:
            with self._lock:
                img = self._latest
            if img is not None:
                break
            await asyncio.sleep(0.005)
        self._recv_count   += 1
        self._recv_wait_ms += (time.perf_counter() - t0) * 1000
        frame = VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base
        return frame

@sio.on('connect')
async def on_connect():
    print(f"Connected to server.")
    if _payload:
        await sio.emit('register_agent', _payload)


@sio.on('disconnect')
async def on_disconnect():
    print("Disconnected from server. Waiting to reconnect...")


@sio.on('connect_error')
async def on_connect_error(data):
    print(f"Connection attempt failed: {data}")


@sio.on('registration_status')
async def on_reg_status(data):
    global _registration_failed, _payload
    print(f"\n==============================")
    print(f"DEVICE: {device_name}")
    if data.get('paired'):
        print(f"STATUS: PAIRED")
        token = data.get('device_token')
        if token:
            save_creds(token)
            # Update payload so future reconnects use the token, not the one-time code
            _payload = {'id': device_id, 'name': device_name, 'device_token': token}
            print("Credentials saved. Future runs will connect automatically.")
    else:
        print(f"STATUS: FAILED — {data.get('message', 'Unknown error')}")
        _registration_failed = True
        await sio.disconnect()  # unblock sio.wait() in main()
    print(f"==============================\n")

@sio.on('force_disconnect')
async def on_force_disconnect(data):
    global _force_disconnected
    print(f"\n[Server] {data.get('message', 'Disconnected by server')}")
    _force_disconnected = True
    if CREDS_FILE.exists():
        CREDS_FILE.unlink()
        print("Credentials cleared — re-pair to reconnect.")
    await sio.disconnect()


@sio.on('signal')
async def on_signal(data):
    global pc
    if not data.get('sdp') or data.get('type') != 'offer':
        return

    # Close any previous connection before starting a new one
    if pc is not None:
        await pc.close()

    pc = RTCPeerConnection(configuration=_build_rtc_config(data.get('ice_servers', [])))
    pc.addTrack(ScreenShareTrack())

    @pc.on("datachannel")
    def on_datachannel(channel):
        _loop = asyncio.get_event_loop()

        @channel.on("message")
        def on_message(message):
            try:
                # Run in thread so pyautogui calls don't block asyncio
                _loop.run_in_executor(None, handle_input, json.loads(message))
            except Exception as e:
                print(f"Input error: {e}")

    offer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
    await pc.setRemoteDescription(offer)
    for t in pc.getTransceivers():
        if t.kind == "video":
            t.direction = "sendonly"
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    sdp = _patch_sdp_bitrate(pc.localDescription.sdp, kbps=1500)
    await sio.emit('signal', {
        'sdp': sdp,
        'type': pc.localDescription.type,
        'target_id': data.get('sender_id')
    })

async def main():
    global _payload, _registration_failed, _force_disconnected
    try:
        while True:
            creds = load_creds()
            if creds:
                print(f"Found saved credentials. Connecting as {device_name}...")
                _payload = {'id': creds['device_id'], 'name': device_name, 'device_token': creds['device_token']}
            else:
                if len(sys.argv) > 1:
                    pairing_code = sys.argv[1].strip()
                    print(f"Using pairing code from argument: {pairing_code}")
                else:
                    pairing_code = input("Enter pairing code from the dashboard: ").strip()
                _payload = {'id': device_id, 'name': device_name, 'pairing_code': pairing_code}

            _registration_failed = False
            _force_disconnected = False

            try:
                # on_connect fires here and emits register_agent automatically
                await sio.connect(SERVER_URL)
            except Exception as e:
                print(f"Could not reach server ({e}). Retrying in 5s...")
                await asyncio.sleep(5)
                continue

            # Blocks here; if the server restarts, the built-in reconnect fires
            # on_connect again which re-emits register_agent — no manual retry needed.
            await sio.wait()

            if _force_disconnected:
                break  # admin kicked us — stop

            if _registration_failed:
                answer = input("Delete saved credentials and re-pair? (y/n): ").strip().lower()
                if answer == 'y':
                    CREDS_FILE.unlink(missing_ok=True)
                    print("Credentials cleared.")
                    continue
                break

            break  # clean exit (e.g. KeyboardInterrupt propagated as disconnect)

    except Exception as e:
        print(f"Error: {e}")
    finally:
        print("Shutting down...")
        try:
            if sio.connected:
                await sio.disconnect()
        except Exception:
            pass
        await asyncio.sleep(0)
        if pc is not None:
            await pc.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
