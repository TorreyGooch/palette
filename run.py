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


def tailscale_ip() -> str | None:
    """This machine's tailnet IPv4, via the CLI or by scanning interfaces.

    Tailscale hands out addresses from the 100.64.0.0/10 CGNAT range.
    """
    import socket

    try:
        out = subprocess.run(["tailscale", "ip", "-4"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.split():
            if line.startswith("100."):
                return line.strip()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       family=socket.AF_INET):
            addr = info[4][0]
            second = int(addr.split(".")[1])
            if addr.startswith("100.") and 64 <= second <= 127:
                return addr
    except Exception:
        pass
    return None


def resolve_host(value: str) -> str:
    if value.lower() in ("tailscale", "tailnet", "ts"):
        ip = tailscale_ip()
        if not ip:
            print("ERROR: PALETTE_HOST=tailscale but no Tailscale IPv4 was found.")
            print("Is Tailscale running? Check with: tailscale ip -4")
            sys.exit(1)
        return ip
    return value


HOST = resolve_host(os.environ.get("PALETTE_HOST", "127.0.0.1"))
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
    if HOST.startswith("100."):
        print(f"  tailnet only — reachable from your other Tailscale devices")
    elif HOST == "0.0.0.0":
        print("  WARNING: bound to every interface, including any local wi-fi.")
        print("  This app has no authentication. For tailnet-only access use:")
        print("    PALETTE_HOST=tailscale")
    if HOST != "127.0.0.1":
        print("  If it is unreachable, Windows Firewall is the usual cause —")
        print(f"  inbound TCP {PORT} must be allowed. See SERVER.md.")
    uvicorn.run("palette_app.main:app", host=HOST, port=PORT, reload=False)
