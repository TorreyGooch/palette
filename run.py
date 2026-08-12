#!/usr/bin/env python3
"""
PALETTE launcher.
Installs dependencies on first run, checks for ffmpeg, starts the server,
and opens the browser automatically.
"""
import os
import sys
import subprocess
import time
import threading
import webbrowser
import shutil

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(TOOL_DIR)


def ensure_deps():
    try:
        import fastapi
        import uvicorn
        import yt_dlp
        import aiofiles
        import multipart  # python-multipart
        import PIL
    except ImportError:
        print("Installing dependencies (first run)...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=TOOL_DIR,
        )
        print("Dependencies installed.\n")


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("=" * 60)
        print("WARNING: ffmpeg not found on PATH.")
        print("Video features (thumbnails, clips, exports) will not work.")
        print("Install ffmpeg: winget install ffmpeg")
        print("=" * 60)
        print()


HOST = os.environ.get("PALETTE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PALETTE_PORT", "7861"))


def open_browser():
    time.sleep(1.8)
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    ensure_deps()
    check_ffmpeg()

    import uvicorn

    if HOST in ("127.0.0.1", "localhost"):
        threading.Thread(target=open_browser, daemon=True).start()
    print(f"PALETTE running at http://{HOST}:{PORT}")
    uvicorn.run("palette_app.main:app", host=HOST, port=PORT, reload=False)
