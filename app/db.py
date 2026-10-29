import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(os.environ.get("SYNC_DB_PATH") or Path(__file__).resolve().parent.parent / "data.sqlite3")
_lock = threading.Lock()
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Africa/Cairo"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS plots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER UNIQUE NOT NULL,
    posted_at TEXT NOT NULL,          -- ISO, local time of the Telegram message
    day_count INTEGER,
    city TEXT, sector TEXT, block TEXT, plot TEXT,
    area REAL, features TEXT, down_payment TEXT,
    project_reserved INTEGER, project_total INTEGER,
    plot_key TEXT NOT NULL,
    -- pending | done | rejected | failed | duplicate
    status TEXT NOT NULL DEFAULT 'pending',
    dashboard_id TEXT,
    note TEXT,
    raw TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plots_key ON plots(plot_key);
CREATE INDEX IF NOT EXISTS plots_status ON plots(status);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
-- Telegram region name -> dashboard المنطقة, learned once per region
CREATE TABLE IF NOT EXISTS region_aliases (
    tg_key TEXT PRIMARY KEY, dash_region TEXT NOT NULL, created_at TEXT NOT NULL
);
-- everything shown to the user: platform events and panel toasts. important=1 also pops a Windows notification
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,               -- new_plot | reserved | review | error | gap | unparsed | panel | ...
    level TEXT NOT NULL,              -- info | success | warning | error
    title TEXT NOT NULL, body TEXT, plot_id INTEGER,
    important INTEGER NOT NULL DEFAULT 0,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notifications_read ON notifications(read, important);
-- unparsed messages the user hid from the panel's alert (still listed on /unparsed)
CREATE TABLE IF NOT EXISTS unparsed_dismissed (msg_id INTEGER PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, plot_id INTEGER, action TEXT NOT NULL, detail TEXT
);
"""


def now() -> str:
    return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_conn = connect()
_conn.executescript(SCHEMA)
# columns added after the first release
_cols = {r[1] for r in _conn.execute("PRAGMA table_info(plots)")}
for col in ("suggest_id", "suggest_region"):
    if col not in _cols:
        _conn.execute(f"ALTER TABLE plots ADD COLUMN {col} TEXT")
_conn.commit()


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def x(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def get_setting(key: str, default=None):
    rows = q("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value) -> None:
    x("INSERT INTO settings(key,value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log(action: str, plot_id: int | None = None, detail: str = "") -> None:
    x("INSERT INTO log(at,plot_id,action,detail) VALUES(?,?,?,?)",
      (now(), plot_id, action, detail))


def insert_plot(msg_id: int, posted_at: str, r: dict, key: str, raw: str) -> int | None:
    """Insert a parsed reservation. Returns the row id, or None if the message was already saved.

    The live handler, catch-up and resync can save the same message concurrently, so dedupe is
    INSERT OR IGNORE on the UNIQUE msg_id, not check-then-insert.
    """
    dup = q("SELECT id FROM plots WHERE plot_key=? AND status!='duplicate' AND msg_id!=? LIMIT 1", (key, msg_id))
    status, note = ("duplicate", f"نفس القطعة اتسجلت قبل كده (#{dup[0]['id']})") if dup else ("pending", None)
    t = now()
    with _lock:
        cur = _conn.execute(
            """INSERT OR IGNORE INTO plots(msg_id,posted_at,day_count,city,sector,block,plot,area,features,
               down_payment,project_reserved,project_total,plot_key,status,note,raw,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (msg_id, posted_at, r["day_count"], r["city"], r["sector"], r["block"], r["plot"],
             r["area"], r["features"], r["down_payment"], r["project_reserved"], r["project_total"],
             key, status, note, raw, t, t),
        )
        _conn.commit()
        return cur.lastrowid if cur.rowcount else None


def log_unparsed(msg_id: int, text: str) -> bool:
    """A message that looks like a reservation but didn't parse. Logged once per message; True if new."""
    if q("SELECT 1 FROM log WHERE action='unparsed' AND detail LIKE ?", (f"msg {msg_id}:%",)):
        return False
    log("unparsed", detail=f"msg {msg_id}: {text}")  # the full text: /unparsed shows it
    return True


def set_unparsed_text(msg_id: int, text: str) -> None:
    """Replace a stored text (older rows kept only the first 200 characters)."""
    x("UPDATE log SET detail=? WHERE action='unparsed' AND detail LIKE ?", (f"msg {msg_id}: {text}", f"msg {msg_id}:%"))


def unparsed(day: str | None = None) -> list[dict]:
    """Every unparsed message (or one day's), newest first, with whether it was hidden or saved as a plot since."""
    rows = q("SELECT at, detail FROM log WHERE action='unparsed'" + (" AND at LIKE ?" if day else "") + " ORDER BY id DESC",
             (day + "%",) if day else ())
    hidden = {r["msg_id"] for r in q("SELECT msg_id FROM unparsed_dismissed")}
    out = []
    for r in rows:
        msg_id, _, text = r["detail"].partition(": ")
        msg_id = int(msg_id.removeprefix("msg "))
        plot = q("SELECT id, plot, city, status FROM plots WHERE msg_id=?", (msg_id,))
        out.append({"at": r["at"], "msg_id": msg_id, "text": text, "dismissed": msg_id in hidden,
                    "plot": plot[0] if plot else None})
    return out


def unparsed_today(day: str) -> list[dict]:
    """The panel's alert: today's unparsed messages, minus those saved as a plot since or hidden by the user."""
    return [u for u in reversed(unparsed(day)) if not u["plot"] and not u["dismissed"]]


def dismiss_unparsed(msg_id: int, hide: bool = True) -> None:
    if hide:
        x("INSERT OR IGNORE INTO unparsed_dismissed(msg_id, at) VALUES(?,?)", (msg_id, now()))
    else:
        x("DELETE FROM unparsed_dismissed WHERE msg_id=?", (msg_id,))


def update_plot(plot_id: int, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE plots SET {cols} WHERE id=?", (*fields.values(), plot_id))


def missing_day_counts(day: str) -> list[int]:
    """Gaps in the channel's daily counter (إجمالي اليوم) = messages we never received."""
    seen = sorted({r["day_count"] for r in q(
        "SELECT day_count FROM plots WHERE posted_at LIKE ? AND day_count IS NOT NULL", (day + "%",))})
    if not seen:
        return []
    return sorted(set(range(seen[0], seen[-1] + 1)) - set(seen))


def new_today(key: str, day: str, items) -> set[str]:
    """Items not seen yet under `key` for `day`, then remembered. Kept in settings so a restart doesn't
    repeat a warning; a new day starts empty."""
    saved_day, _, saved = (get_setting(key, "") or "").partition(":")
    seen = set(saved.split(",")) - {""} if saved_day == day else set()
    new = {str(i) for i in items} - seen
    if new:
        set_setting(key, f"{day}:" + ",".join(sorted(seen | new)))
    return new


def region_key(city: str, sector: str) -> str:
    from .parser import normalize
    return f"{normalize(city)}|{normalize(sector)}"


def get_region_alias(city: str, sector: str) -> str | None:
    rows = q("SELECT dash_region FROM region_aliases WHERE tg_key=?", (region_key(city, sector),))
    return rows[0]["dash_region"] if rows else None


def set_region_alias(city: str, sector: str, dash_region: str) -> None:
    x("INSERT INTO region_aliases(tg_key,dash_region,created_at) VALUES(?,?,?) "
      "ON CONFLICT(tg_key) DO UPDATE SET dash_region=excluded.dash_region",
      (region_key(city, sector), dash_region, now()))
