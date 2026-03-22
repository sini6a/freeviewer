"""FreeViewer Agent — GUI version."""

import asyncio
import ctypes
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="Unclosed client session")
warnings.filterwarnings("ignore", message="Unclosed connector")

import cv2
import numpy as np
import pyautogui
import pystray
import socketio as _sio_module
import tkinter as tk
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import VideoFrame
from dotenv import load_dotenv
from mss import mss
from PIL import Image, ImageDraw
from pynput.keyboard import Controller, Key

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


# ── Require admin — relaunch elevated if needed ────────────────────────────────
def _ensure_admin():
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable,
                " ".join(f'"{a}"' for a in sys.argv),
                None, 1,
            )
            sys.exit(0)
    except Exception:
        pass

_ensure_admin()

# ── DPI awareness ──────────────────────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── Constants ──────────────────────────────────────────────────────────────────
keyboard_ctrl   = Controller()
screen_width, screen_height = pyautogui.size()
device_name     = socket.gethostname()
device_id       = f"dev_{device_name.lower()}"
SERVER_DEFAULT  = os.environ.get("SERVER_URL", "http://127.0.0.1:5000")
CREDS_FILE      = Path(__file__).parent / "agent_creds.json"
SETTINGS_FILE   = Path(__file__).parent / "agent_settings.json"

DEFAULT_SETTINGS = {
    "server_url": SERVER_DEFAULT,
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            # Fill in any missing keys with defaults
            return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))
MOUSE_BUTTONS   = {0: "left", 1: "middle", 2: "right"}

CODE_TO_KEY = {
    **{f"Key{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{f"Digit{n}": n for n in "0123456789"},
    **{f"Numpad{n}": n for n in "0123456789"},
    **{f"F{n}": getattr(Key, f"f{n}") for n in range(1, 13)},
    "Space": Key.space,         "Enter": Key.enter,         "NumpadEnter": Key.enter,
    "Backspace": Key.backspace, "Tab": Key.tab,             "Escape": Key.esc,
    "Delete": Key.delete,       "Insert": Key.insert,       "CapsLock": Key.caps_lock,
    "Home": Key.home,           "End": Key.end,
    "PageUp": Key.page_up,      "PageDown": Key.page_down,
    "ArrowUp": Key.up,          "ArrowDown": Key.down,
    "ArrowLeft": Key.left,      "ArrowRight": Key.right,
    "ShiftLeft": Key.shift_l,   "ShiftRight": Key.shift_r,
    "ControlLeft": Key.ctrl_l,  "ControlRight": Key.ctrl_r,
    "AltLeft": Key.alt_l,       "AltRight": Key.alt_r,
    "MetaLeft": Key.cmd,        "MetaRight": Key.cmd,
    "Minus": "-",      "Equal": "=",       "BracketLeft": "[",  "BracketRight": "]",
    "Backslash": "\\", "Semicolon": ";",   "Quote": "'",        "Backquote": "`",
    "Comma": ",",      "Period": ".",       "Slash": "/",
    "NumpadAdd": "+",  "NumpadSubtract": "-", "NumpadMultiply": "*",
    "NumpadDivide": "/", "NumpadDecimal": ".",
}

# ── Colours ────────────────────────────────────────────────────────────────────
BG       = "#0f172a"
CARD     = "#1e293b"
BORDER   = "#334155"
ACCENT   = "#3b82f6"
SUCCESS  = "#10b981"
WARNING  = "#f59e0b"
DANGER   = "#ef4444"
TEXT     = "#e2e8f0"
MUTED    = "#64748b"
MUTED2   = "#94a3b8"

# Map hex colour to RGB tuple for PIL
_HEX_TO_RGB = {
    SUCCESS: (16, 185, 129),
    WARNING: (245, 158, 11),
    DANGER:  (239, 68, 68),
    ACCENT:  (59, 130, 246),
}


# ── UAC helpers ────────────────────────────────────────────────────────────────
def uac_enabled() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
        )
        val, _ = winreg.QueryValueEx(key, "EnableLUA")
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return False


def open_uac_settings():
    try:
        subprocess.Popen(
            ["reg", "add",
             r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
             "/v", "EnableLUA", "/t", "REG_DWORD", "/d", "0", "/f"],
            shell=True,
        )
    except Exception:
        pass


# ── Input handler ──────────────────────────────────────────────────────────────
def handle_input(msg: dict):
    t = msg.get("type")
    try:
        if t in ("mousedown", "mouseup"):
            btn = MOUSE_BUTTONS.get(msg.get("button", 0), "left")
            x, y = int(msg["x"] * screen_width), int(msg["y"] * screen_height)
            (pyautogui.mouseDown if t == "mousedown" else pyautogui.mouseUp)(x, y, button=btn)
        elif t == "scroll":
            x, y = int(msg["x"] * screen_width), int(msg["y"] * screen_height)
            dy = msg.get("deltaY", 0)
            if dy:
                pyautogui.scroll(-1 if dy > 0 else 1, x, y)
        elif t == "mousemove":
            pyautogui.moveTo(int(msg["x"] * screen_width), int(msg["y"] * screen_height))
        elif t in ("keydown", "keyup"):
            pk = CODE_TO_KEY.get(msg.get("code", ""))
            if pk is not None:
                (keyboard_ctrl.press if t == "keydown" else keyboard_ctrl.release)(pk)
    except Exception:
        pass


# ── Screen capture ─────────────────────────────────────────────────────────────
class ScreenShareTrack(VideoStreamTrack):
    """Captures the screen in a background thread so recv() never blocks asyncio."""

    STATS_INTERVAL = 10.0  # seconds between log entries

    def __init__(self, max_width: int, max_height: int, framerate: int,
                 log_cb=None):
        super().__init__()
        self._max_width  = max_width
        self._max_height = max_height
        self._framerate  = framerate
        self._latest     = None
        self._lock       = threading.Lock()
        self._running    = True
        self._log        = log_cb or (lambda msg: None)
        # recv() stats
        self._recv_count   = 0
        self._recv_wait_ms = 0.0
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def stop(self):
        self._running = False
        super().stop()

    def _capture_loop(self):
        sct = mss()
        monitor = sct.monitors[1]
        interval  = 1.0 / self._framerate
        # stats accumulators
        cap_count  = 0
        cap_ms     = 0.0
        over_count = 0          # frames where capture exceeded interval
        stats_t    = time.perf_counter()
        out_w = out_h = 0

        while self._running:
            t0  = time.perf_counter()
            img = np.array(sct.grab(monitor))
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            h, w = img.shape[:2]
            scale = min(self._max_width / w, self._max_height / h, 1.0)
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

            # Periodic stats log
            now = time.perf_counter()
            if now - stats_t >= self.STATS_INTERVAL and cap_count:
                actual_fps = cap_count / (now - stats_t)
                avg_ms     = cap_ms / cap_count
                recv_avg   = (self._recv_wait_ms / self._recv_count
                              if self._recv_count else 0)
                self._log(
                    f"[capture] {actual_fps:.1f} fps  "
                    f"grab+encode {avg_ms:.0f} ms/frame  "
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
        wait_ms = (time.perf_counter() - t0) * 1000
        self._recv_count    += 1
        self._recv_wait_ms  += wait_ms
        frame = VideoFrame.from_ndarray(img, format="bgr24")
        frame.pts, frame.time_base = pts, time_base
        return frame


# ── Credential helpers ─────────────────────────────────────────────────────────
def load_creds():
    if CREDS_FILE.exists():
        try:
            return json.loads(CREDS_FILE.read_text())
        except Exception:
            pass
    return None


def save_creds(token: str):
    CREDS_FILE.write_text(json.dumps({"device_id": device_id, "device_token": token}))


# ── Helpers ────────────────────────────────────────────────────────────────────
def make_entry(parent, textvariable=None, font=("Segoe UI", 10), **kw):
    """Styled entry with a 1-px border frame."""
    border = tk.Frame(parent, bg=BORDER)
    border.pack(fill="x", pady=(2, 0))
    inner = tk.Frame(border, bg=CARD)
    inner.pack(fill="x", padx=1, pady=1)
    e = tk.Entry(inner, textvariable=textvariable, bg=CARD, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=font,
                 highlightthickness=0, bd=0, **kw)
    e.pack(fill="x", ipady=7, ipadx=10)
    e.bind("<FocusIn>",  lambda _: border.configure(bg=ACCENT))
    e.bind("<FocusOut>", lambda _: border.configure(bg=BORDER))
    return e


def make_btn(parent, text, command, bg=ACCENT, fg="white", **kw):
    kw.setdefault("padx", 14)
    kw.setdefault("pady", 8)
    kw.setdefault("font", ("Segoe UI", 9, "bold"))
    return tk.Button(parent, text=text, command=command,
                     bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                     relief="flat", cursor="hand2", bd=0, **kw)


def label(parent, text, fg=MUTED2, font=("Segoe UI", 8, "bold"), **kw):
    kw.setdefault("bg", BG)
    return tk.Label(parent, text=text, fg=fg, font=font, **kw)


# ── Tray icon image ────────────────────────────────────────────────────────────
def _make_tray_image(dot_color_hex: str) -> Image.Image:
    """64×64 PIL image: dark background with a coloured status dot."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Background square with rounded feel (just fill entire canvas)
    d.rectangle([0, 0, size, size], fill=(15, 23, 42, 255))   # #0f172a
    # "FV" letters
    d.rectangle([8, 10, 28, 30], fill=(59, 130, 246, 255))    # blue accent block
    # Status dot (bottom-right)
    rgb = _HEX_TO_RGB.get(dot_color_hex, (100, 116, 139))
    d.ellipse([38, 38, 58, 58], fill=(*rgb, 255))
    return img


# ── UAC warning dialog ─────────────────────────────────────────────────────────
def show_uac_dialog(root: tk.Tk):
    win = tk.Toplevel(root)
    win.title("UAC Warning — FreeViewer")
    win.configure(bg=BG)
    win.resizable(False, False)
    win.geometry("430x300")
    win.transient(root)
    win.grab_set()

    tk.Label(win, text="⚠", bg=BG, fg=WARNING,
             font=("Segoe UI", 36)).pack(pady=(24, 6))
    tk.Label(win, text="User Account Control is enabled",
             bg=BG, fg=TEXT, font=("Segoe UI", 12, "bold")).pack()
    tk.Label(win,
             text=(
                 "FreeViewer requires UAC to be disabled so it can\n"
                 "send input to all windows, including those running\n"
                 "as Administrator (e.g. Task Manager, installers).\n\n"
                 "Without this, elevated windows cannot be controlled."
             ),
             bg=BG, fg=MUTED2, font=("Segoe UI", 9),
             justify="center").pack(pady=(10, 20))

    row = tk.Frame(win, bg=BG)
    row.pack(pady=(0, 10))
    make_btn(row, "Disable UAC & Restart",
             command=lambda: [open_uac_settings(), win.destroy()],
             bg="#7c3aed", padx=24, pady=12,
             font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
    make_btn(row, "Continue Anyway",
             command=win.destroy, bg=CARD, fg=TEXT,
             padx=24, pady=12,
             font=("Segoe UI", 10)).pack(side="left", padx=8)

    win.wait_window()


# ── Main application ───────────────────────────────────────────────────────────
class AgentApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("FreeViewer Agent")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.geometry("460x590")

        self._settings = load_settings()
        self._q: queue.Queue = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._sio = None
        self._pc = None
        self._track: ScreenShareTrack | None = None
        self._payload = None
        self._stopping = False
        self._tray: pystray.Icon | None = None
        self._current_status_color = WARNING

        self._build_ui()
        self._setup_tray()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.root.after(100, self._poll)

    # ── Async thread ───────────────────────────────────────────────────────────
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── Queue polling (main thread) ────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "log":
                    self._append_log(data)
                elif kind == "status":
                    self._set_status(*data)
                elif kind == "show_pair":
                    self._pair_section.pack(after=self._url_sec, fill="x", padx=24, pady=(0, 12))
                elif kind == "hide_pair":
                    self._pair_section.pack_forget()
                elif kind == "disc_enable":
                    self._disc_btn.configure(state="normal")
                elif kind == "ask_repair":
                    self._ask_repair()
                elif kind == "set_url":
                    self._url_var.set(data)
        except queue.Empty:
            pass
        if not self._stopping:
            self.root.after(100, self._poll)

    # ── Thread-safe emit from async ────────────────────────────────────────────
    def _emit(self, kind, *args):
        self._q.put((kind, args))

    def alog(self, msg: str):
        self._q.put(("log", msg))

    def astatus(self, text: str, color: str):
        self._q.put(("status", (text, color)))

    # ── Tray icon ──────────────────────────────────────────────────────────────
    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("FreeViewer Agent", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show", self._show_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._quit_app),
        )
        self._tray = pystray.Icon(
            "FreeViewer",
            _make_tray_image(WARNING),
            f"FreeViewer Agent — {device_name}",
            menu,
        )
        self._tray.run_detached()

    def _update_tray_icon(self, color: str):
        if self._tray:
            self._tray.icon = _make_tray_image(color)

    def _show_window(self, icon=None, item=None):
        self.root.after(0, self._do_show_window)

    def _do_show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit_app(self, icon=None, item=None):
        self._stopping = True
        if self._tray:
            self._tray.stop()
        self._submit(self._do_disconnect())
        self.root.after(0, lambda: self.root.after(600, self.root.destroy))

    # ── Build UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = self.root

        # Header
        hdr = tk.Frame(root, bg=BG)
        hdr.pack(fill="x", padx=24, pady=(20, 16))

        icon_bg = tk.Frame(hdr, bg=ACCENT, width=40, height=40)
        icon_bg.pack_propagate(False)
        icon_bg.pack(side="left")
        tk.Label(icon_bg, text="⬛", bg=ACCENT, fg="white",
                 font=("Segoe UI", 16)).place(relx=.5, rely=.5, anchor="center")

        info = tk.Frame(hdr, bg=BG)
        info.pack(side="left", padx=(12, 0))
        tk.Label(info, text="FreeViewer Agent", bg=BG, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(info, text=device_name, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")

        # Minimize to tray button (top-right)
        hdr_right = tk.Frame(hdr, bg=BG)
        hdr_right.pack(side="right")
        make_btn(hdr_right, "⊟  Hide to tray", self._on_close,
                 bg=CARD, fg=MUTED2, padx=10, pady=5).pack()

        # Status card
        sc = tk.Frame(root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        sc.pack(fill="x", padx=24, pady=(0, 16))
        inner_sc = tk.Frame(sc, bg=CARD)
        inner_sc.pack(fill="x", padx=16, pady=12)
        self._dot = tk.Label(inner_sc, text="●", bg=CARD, fg=WARNING,
                             font=("Segoe UI", 11))
        self._dot.pack(side="left")
        self._status_lbl = tk.Label(inner_sc, text="Starting…", bg=CARD, fg=TEXT,
                                    font=("Segoe UI", 10, "bold"))
        self._status_lbl.pack(side="left", padx=(7, 0))

        # Settings card
        settings_card = tk.Frame(root, bg=CARD,
                                 highlightbackground=BORDER, highlightthickness=1)
        settings_card.pack(fill="x", padx=24, pady=(0, 12))
        settings_inner = tk.Frame(settings_card, bg=CARD)
        settings_inner.pack(fill="x", padx=14, pady=10)

        # Header row with toggle
        sh = tk.Frame(settings_inner, bg=CARD)
        sh.pack(fill="x")
        tk.Label(sh, text="SETTINGS", bg=CARD, fg=MUTED2,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        self._settings_toggle_btn = tk.Button(
            sh, text="▲ Hide", bg=CARD, fg=MUTED, relief="flat",
            font=("Segoe UI", 8), cursor="hand2", bd=0,
            activebackground=CARD, activeforeground=MUTED2,
            command=self._toggle_settings)
        self._settings_toggle_btn.pack(side="right")

        # Collapsible body
        self._settings_body = tk.Frame(settings_inner, bg=CARD)
        self._settings_body.pack(fill="x", pady=(8, 0))

        # Server URL row
        label(self._settings_body, "SERVER URL", bg=CARD).pack(anchor="w")
        self._url_var = tk.StringVar(value=self._settings["server_url"])
        url_border = tk.Frame(self._settings_body, bg=BORDER)
        url_border.pack(fill="x", pady=(2, 8))
        url_inner = tk.Frame(url_border, bg=CARD)
        url_inner.pack(fill="x", padx=1, pady=1)
        url_entry = tk.Entry(url_inner, textvariable=self._url_var,
                             bg=CARD, fg=TEXT, insertbackground=TEXT,
                             relief="flat", font=("Segoe UI", 9),
                             highlightthickness=0, bd=0)
        url_entry.pack(fill="x", ipady=5, ipadx=8)
        url_entry.bind("<FocusIn>",  lambda _: url_border.configure(bg=ACCENT))
        url_entry.bind("<FocusOut>", lambda _: url_border.configure(bg=BORDER))

        # Save button
        make_btn(self._settings_body, "Save", self._on_save_settings,
                 padx=14, pady=5).pack(anchor="w", pady=(0, 8))

        # Keep a ref for show_pair ordering
        self._url_sec = settings_card
        self._settings_visible = True

        # Pairing code — always packed here to fix ordering, hidden later if already paired
        self._pair_section = tk.Frame(root, bg=BG)
        self._pair_section.pack(fill="x", padx=24, pady=(0, 12))
        label(self._pair_section, "PAIRING CODE").pack(anchor="w")
        pair_row = tk.Frame(self._pair_section, bg=BG)
        pair_row.pack(fill="x", pady=(2, 0))
        self._code_var = tk.StringVar()
        code_entry = tk.Entry(
            pair_row, textvariable=self._code_var,
            bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Courier New", 15, "bold"), width=10,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        code_entry.pack(side="left", ipady=7, ipadx=10, fill="x", expand=True, padx=(0, 8))
        self._pair_btn = make_btn(pair_row, "Connect", self._on_pair_click)
        self._pair_btn.pack(side="left")

        # Log
        log_sec = tk.Frame(root, bg=BG)
        log_sec.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        label(log_sec, "LOG").pack(anchor="w", pady=(0, 3))
        log_border = tk.Frame(log_sec, bg=CARD,
                              highlightbackground=BORDER, highlightthickness=1)
        log_border.pack(fill="both", expand=True)
        self._log_txt = tk.Text(
            log_border, bg=CARD, fg=TEXT, font=("Consolas", 9),
            relief="flat", state="disabled", wrap="word",
            insertbackground=TEXT, bd=0, padx=10, pady=8,
        )
        sb = tk.Scrollbar(log_border, command=self._log_txt.yview,
                          bg=CARD, troughcolor=CARD, bd=0, relief="flat")
        self._log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_txt.pack(fill="both", expand=True)

        # Footer
        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=24, pady=(0, 20))
        self._disc_btn = make_btn(foot, "Disconnect", self._on_disc_click,
                                  bg=CARD, fg=DANGER, state="disabled")
        self._disc_btn.pack(side="left")

        # Closing the window always hides to tray
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Hide pairing section immediately if credentials already exist
        if load_creds():
            self._pair_section.pack_forget()

    # ── UI helpers ─────────────────────────────────────────────────────────────
    def _set_status(self, text: str, color: str):
        self._dot.configure(fg=color)
        self._status_lbl.configure(text=text)
        self._current_status_color = color
        self._update_tray_icon(color)
        if self._tray:
            self._tray.title = f"FreeViewer — {text}"

    def _append_log(self, msg: str):
        self._log_txt.configure(state="normal")
        self._log_txt.insert("end", f"› {msg}\n")
        self._log_txt.see("end")
        self._log_txt.configure(state="disabled")

    def _ask_repair(self):
        import tkinter.messagebox as mb
        # Make sure window is visible when asking
        self._do_show_window()
        yes = mb.askyesno(
            "Re-pair Device",
            "The saved credentials were rejected by the server.\n\n"
            "Delete them and re-pair with a new pairing code?",
            parent=self.root,
        )
        if yes:
            CREDS_FILE.unlink(missing_ok=True)
            self._append_log("Credentials cleared. Enter a new pairing code.")
            self._q.put(("show_pair", ()))
            self._pair_btn.configure(state="normal")
        else:
            self._set_status("Stopped", DANGER)

    def _toggle_settings(self):
        if self._settings_visible:
            self._settings_body.pack_forget()
            self._settings_toggle_btn.configure(text="▼ Show")
        else:
            self._settings_body.pack(fill="x", pady=(8, 0))
            self._settings_toggle_btn.configure(text="▲ Hide")
        self._settings_visible = not self._settings_visible

    def _on_save_settings(self):
        self._settings["server_url"] = self._url_var.get().strip()
        save_settings(self._settings)
        self._append_log(f"Settings saved — {self._settings['server_url']}")

    def _on_pair_click(self):
        code = self._code_var.get().strip()
        if not code:
            import tkinter.messagebox as mb
            mb.showwarning("Missing Code", "Please enter the pairing code.", parent=self.root)
            return
        self._pair_btn.configure(state="disabled")
        self._submit(self._connect_with_code(code))

    def _on_disc_click(self):
        self._submit(self._do_disconnect())

    def _on_close(self):
        """Hide to system tray instead of closing."""
        self.root.withdraw()

    # ── Async agent logic ──────────────────────────────────────────────────────
    async def _do_disconnect(self):
        if self._track is not None:
            self._track.stop()
            self._track = None
        if self._pc is not None:
            try:
                await asyncio.wait_for(self._pc.close(), timeout=3)
            except Exception:
                pass
            self._pc = None
        if self._sio and self._sio.connected:
            await self._sio.disconnect()

    async def _connect_loop(self):
        # Sync URL field to saved setting on start
        self._q.put(("set_url", self._settings["server_url"]))
        creds = load_creds()
        if creds:
            self.alog("Found saved credentials.")
            self._payload = {
                "id": creds["device_id"],
                "name": device_name,
                "device_token": creds["device_token"],
            }
            self._q.put(("hide_pair", ()))
            await self._start_sio()
        else:
            self.astatus("Waiting for pairing code…", WARNING)
            self.alog("No credentials found. Enter a pairing code to get started.")
            self._q.put(("show_pair", ()))

    async def _connect_with_code(self, code: str):
        self._payload = {"id": device_id, "name": device_name, "pairing_code": code}
        await self._start_sio()

    async def _start_sio(self):
        self.astatus("Connecting…", WARNING)
        server_url = self._settings.get("server_url", self._url_var.get()).strip()

        sio = _sio_module.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=2,
            reconnection_delay_max=60,
        )
        self._sio = sio

        @sio.on("connect")
        async def on_connect():
            self.astatus("Connected", SUCCESS)
            self.alog("Connected to server.")
            if self._payload:
                await sio.emit("register_agent", self._payload)

        @sio.on("disconnect")
        async def on_disconnect():
            self.astatus("Reconnecting…", WARNING)
            self.alog("Disconnected. Waiting to reconnect…")

        @sio.on("connect_error")
        async def on_connect_error(data):
            self.alog(f"Connection attempt failed: {data}")

        @sio.on("registration_status")
        async def on_reg_status(data):
            if data.get("paired"):
                self.alog(f"Registered as {device_name}.")
                self.astatus("Paired & Online", SUCCESS)
                token = data.get("device_token")
                if token:
                    save_creds(token)
                    self._payload = {"id": device_id, "name": device_name, "device_token": token}
                self._q.put(("hide_pair", ()))
                self._q.put(("disc_enable", ()))
            else:
                msg = data.get("message", "Unknown error")
                self.alog(f"Registration failed: {msg}")
                self.astatus("Failed", DANGER)
                if "token" in msg.lower():
                    self._q.put(("ask_repair", ()))
                await sio.disconnect()

        @sio.on("force_disconnect")
        async def on_force_disconnect(data):
            self.alog(f"Server: {data.get('message', 'Disconnected by server')}")
            self.astatus("Removed by server", DANGER)
            CREDS_FILE.unlink(missing_ok=True)
            await sio.disconnect()

        @sio.on("signal")
        async def on_signal(data):
            if not data.get("sdp") or data.get("type") != "offer":
                return
            self.alog("Incoming connection request — setting up WebRTC…")

            # Stop capture thread immediately before anything else
            if self._track is not None:
                self._track.stop()
                self._track = None

            if self._pc is not None:
                self.alog("Closing previous WebRTC session.")
                try:
                    await asyncio.wait_for(self._pc.close(), timeout=3)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                self._pc = None

            try:
                pc = RTCPeerConnection(
                    configuration=_build_rtc_config(data.get("ice_servers", []))
                )
                self._pc = pc
                capture = data.get("capture_settings", {})
                max_w = capture.get("max_width") or 3840
                max_h = capture.get("max_height") or 2160
                fps   = capture.get("fps") or 30
                track = ScreenShareTrack(
                    max_width=max_w,
                    max_height=max_h,
                    framerate=fps,
                    log_cb=self.alog,
                )
                self._track = track
                pc.addTrack(track)

                @pc.on("connectionstatechange")
                async def on_conn_state():
                    self.alog(f"WebRTC state: {pc.connectionState}")
                    if pc.connectionState in ("failed", "closed"):
                        self.astatus("Paired & Online", SUCCESS)

                @pc.on("datachannel")
                def on_dc(channel):
                    _loop = asyncio.get_event_loop()

                    @channel.on("message")
                    def on_msg(message):
                        try:
                            # Run in thread so pyautogui calls don't block asyncio
                            _loop.run_in_executor(None, handle_input, json.loads(message))
                        except Exception:
                            pass

                await pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type=data["type"]))
                for t in pc.getTransceivers():
                    if t.kind == "video":
                        t.direction = "sendonly"
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                # Patch SDP to allow higher video bitrate (default is ~300 kbps)
                sdp = pc.localDescription.sdp
                sdp = _patch_sdp_bitrate(sdp, kbps=1500)
                await sio.emit("signal", {
                    "sdp": sdp,
                    "type": pc.localDescription.type,
                    "target_id": data.get("sender_id"),
                })
                self.alog("Answer sent — streaming.")
                self.astatus("Streaming", ACCENT)
            except Exception as e:
                self.alog(f"WebRTC setup error: {e}")
                self.astatus("Paired & Online", SUCCESS)

        while True:
            try:
                await sio.connect(server_url)
                break
            except Exception as e:
                self.alog(f"Could not reach server ({e}). Retrying in 5s…")
                await asyncio.sleep(5)

        await sio.wait()

    # ── Run ────────────────────────────────────────────────────────────────────
    def run(self):
        if uac_enabled():
            show_uac_dialog(self.root)
        self._submit(self._connect_loop())
        # Start in tray if already paired — no need to show window
        if load_creds():
            self.root.withdraw()
        self.root.mainloop()


if __name__ == "__main__":
    # ── Single-instance guard ──────────────────────────────────────────────────
    _MUTEX_NAME = "FreeViewerAgent_SingleInstance"
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = ctypes.windll.user32.FindWindowW(None, "FreeViewer Agent")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)       # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        sys.exit(0)
    # ── Launch ─────────────────────────────────────────────────────────────────
    AgentApp().run()
