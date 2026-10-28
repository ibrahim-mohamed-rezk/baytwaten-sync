"""Notifications: every event goes to the `notifications` table (the /notifications page lists them);
important ones also pop up in the Windows notification bar, even with no browser open.

Windows toasts go through PowerShell's built-in WinRT toast API (no extra dependency). The script is passed
with -EncodedCommand (UTF-16), so Arabic survives PowerShell 5.1's ANSI code page. Clicking a toast opens
the notifications page.
"""
import base64
import logging
import os
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

from . import db

log = logging.getLogger("notify")
# PowerShell's own app id: Windows only shows toasts from registered apps, and this one always is.
AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_last: dict[str, float] = {}  # throttle key -> last time it was sent


def page_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('PANEL_PORT', 8800)}/notifications"


def windows_enabled() -> bool:
    return sys.platform == "win32" and db.get_setting("win_notify", "1") == "1"


def notify(kind: str, title: str, body: str = "", level: str = "info", plot_id: int | None = None,
           important: bool = False, throttle: str | None = None, every: float = 600) -> int | None:
    """Record a notification; important ones also go to Windows.

    throttle: a key for repeating problems (dashboard down, resync failing). Within `every` seconds of the
    last one with the same key, nothing is recorded or shown.
    """
    if throttle:
        now = time.monotonic()
        if now - _last.get(throttle, -every) < every:
            return None
        _last[throttle] = now
    nid = db.x("INSERT INTO notifications(at,kind,level,title,body,plot_id,important) VALUES(?,?,?,?,?,?,?)",
               (db.now(), kind, level, title, body or None, plot_id, int(important)))
    if important and windows_enabled():
        threading.Thread(target=show_windows_toast, args=(title, body), daemon=True).start()
    return nid


def show_windows_toast(title: str, body: str = "") -> bool:
    text = lambda s: escape(" ".join((s or "").split()), {'"': "&quot;"})  # noqa: E731  one line, XML-safe
    xml = (f'<toast activationType="protocol" launch="{text(page_url())}"><visual><binding template="ToastGeneric">'
           f"<text>{text(title)}</text><text>{text(body)}</text></binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml('{xml.replace(chr(39), chr(39) * 2)}')\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{AUMID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($xml))\n"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16-le")).decode()],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode:
            log.warning("windows toast failed: %s", r.stderr.decode(errors="replace")[:300])
        return r.returncode == 0
    except Exception as e:
        log.warning("windows toast failed: %s", e)
        return False


def unread_important() -> int:
    return db.q("SELECT COUNT(*) n FROM notifications WHERE read=0 AND important=1")[0]["n"]
