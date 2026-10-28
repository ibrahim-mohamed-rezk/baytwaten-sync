import os
import webbrowser

import uvicorn
from dotenv import load_dotenv

from app.console import utf8_console

utf8_console()
load_dotenv()
port = int(os.environ.get("PANEL_PORT", 8800))
webbrowser.open(f"http://localhost:{port}")
uvicorn.run("app.main:app", host="127.0.0.1", port=port)
