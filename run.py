import os
import socket
import threading
import time
import webbrowser

import uvicorn
from dotenv import load_dotenv

from app.console import utf8_console

utf8_console()
load_dotenv()
port = int(os.environ.get("PANEL_PORT", 8800))
url = f"http://localhost:{port}"


def listening() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


if listening():  # already running (e.g. the desktop icon clicked twice): just show it
    webbrowser.open(url)
    raise SystemExit


def open_when_ready():
    for _ in range(60):
        if listening():
            webbrowser.open(url)
            return
        time.sleep(0.5)


threading.Thread(target=open_when_ready, daemon=True).start()
uvicorn.run("app.main:app", host="127.0.0.1", port=port)
