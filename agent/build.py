"""
Build FreeViewer-Agent.exe
Run from the agent/ directory:  python build.py

Requirements:
    pip install pyinstaller
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",                       # no console window
    "--name", "FreeViewer-Agent",
    "--clean",
    "--noconfirm",
    "--uac-admin",                      # request elevation on launch
    # aiortc ships compiled codecs that PyInstaller misses
    "--collect-all", "aiortc",
    "--collect-all", "av",
    # other packages with data files / dynamic imports
    "--hidden-import", "cv2",
    "--hidden-import", "mss",
    "--hidden-import", "pynput.keyboard",
    "--hidden-import", "pynput.mouse",
    "--hidden-import", "pyautogui",
    "--hidden-import", "engineio.async_drivers.aiohttp",
    "--hidden-import", "socketio",
    "--hidden-import", "pystray",
    "--hidden-import", "PIL",
    "--hidden-import", "PIL.Image",
    "--hidden-import", "PIL.ImageDraw",
    "--collect-all", "pystray",
    # bundle the .env so the exe picks up SERVER_URL
    "--add-data", f"{HERE / '.env'}{';' if sys.platform == 'win32' else ':'}.",
    str(HERE / "agent_gui.py"),
]

print("Building FreeViewer-Agent.exe …\n")
result = subprocess.run(cmd)

if result.returncode == 0:
    exe = HERE / "dist" / "FreeViewer-Agent.exe"
    print(f"\n✓ Done!  →  {exe}")
else:
    print("\n✗ Build failed. Check the output above.")
    sys.exit(result.returncode)
