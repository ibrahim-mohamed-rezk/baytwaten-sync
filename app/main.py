import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import dashboard, db  # noqa: E402
from .notify import notify, show_windows_toast, unread_important, windows_enabled  # noqa: E402
from .listener import Listener  # noqa: E402
from .parser import _AR_NORMALIZE, normalize, why_unparsed  # noqa: E402

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent / "static"


def _label(plot: dict) -> str:
    return f"{plot['city']} – رقم {plot['plot']}"


def _get_plot(plot_id: int) -> dict:
    rows = db.q("SELECT * FROM plots WHERE id=?", (plot_id,))
    if not rows:
        raise HTTPException(404)
    return rows[0]


SEARCH_TTL = 60      # seconds a search result is reused within a batch
JOB_DELAY = 0.3      # pause between background jobs so we don't hammer the dashboard
RECHECK_EVERY = 300  # re-verify rows still pending (someone may reserve them by hand later)
COMPARE_EVERY = 900  # compare today's dashboard reservations with Telegram, warn about unexplained ones
_search_cache: dict[str, tuple[float, list[dict]]] = {}


async def _search(number: str, cached: bool) -> list[dict]:
    hit = _search_cache.get(number)
    if cached and hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return hit[1]
    rows = await dashboard.search(number)
    _search_cache[number] = (time.monotonic(), rows)
    return rows


async def find_target(plot: dict, force_id: str | None = None,
                      cached: bool = False) -> tuple[dict | None, bool, dict | None, str | None]:
    """Read-only lookup of the dashboard plot for a Telegram row.

    Returns (target, confident, suggestion, reason). target is set only for a confident single match
    (or force_id); suggestion is the lone candidate in a differently named region; reason says why
    there's no target. Network/auth errors propagate.
    """
    found = await _search(plot["plot"], cached)
    if force_id:
        target = next((c for c in found if c["id"] == force_id), None)
        if not target:
            return None, False, None, f"القطعة #{force_id} مش موجودة في الداشبورد"
        return target, True, None, None
    pool, confident = dashboard.match(plot, found, db.get_region_alias(plot["city"], plot["sector"]))
    if not pool:
        return None, False, None, f"مفيش قطعة رقم {plot['plot']} في {plot['city']} على الداشبورد"
    if len(pool) > 1:
        return None, False, None, "لقيت أكتر من قطعة مطابقة: " + "، ".join(
            f"#{c['id']} {c['region']} {c['area']}م²" for c in pool)
    if not confident:
        return None, False, pool[0], None
    return pool[0], True, None, None


def _mark_unsure(plot: dict, c: dict) -> None:
    """'failed' with a suggestion; the note says what differs so the user can judge before "اربط وحدّث"."""
    why = []
    if not dashboard.area_matches(c, plot):
        why.append(f"المساحة في الداشبورد {c['area']}م² وفي التليجرام {plot['area']:g}م²")
    if normalize(c["region"]) != normalize(plot["sector"]):
        why.append(f"المنطقة في الداشبورد اسمها \"{c['region']}\" ({c['area']}م²)")
    db.update_plot(plot["id"], status="failed", suggest_id=c["id"], suggest_region=c["region"],
                   note=f"القطعة #{c['id']} ({c['plot']}): " + "، و".join(why)
                        + ". لو هي نفس القطعة اضغط \"اربط وحدّث\"")


async def approve(plot_id: int, by: str = "manual", force_id: str | None = None,
                  cached: bool = False) -> dict:
    """Find the plot in the dashboard and mark it reserved.

    Only acts when the match is certain; anything else becomes 'failed' with a note
    (and a suggested dashboard plot when there's exactly one candidate in another region).
    """
    plot = _get_plot(plot_id)
    if plot["status"] == "done":
        return plot
    try:
        target, _, c, reason = await find_target(plot, force_id, cached)
        if c:
            _mark_unsure(plot, c)
            db.log("approve_unsure", plot_id, f"{by}: suggest #{c['id']}")
            notify("review", f"محتاجة مراجعة: {_label(plot)}", _get_plot(plot_id)["note"], level="warning",
                   plot_id=plot_id, important=by != "manual")
            return _get_plot(plot_id)
        if not target:
            raise LookupError(reason)

        if target["status"] == "reserved":
            note = "كانت محجوزة أصلاً في الداشبورد"
        elif target["status"] != "available":
            raise LookupError(f"حالة القطعة في الداشبورد \"{target['status']}\"، مش متاحة")
        else:
            await dashboard.reserve(target["id"])
            _search_cache.pop(plot["plot"], None)
            note = None
        db.update_plot(plot_id, status="done", dashboard_id=target["id"], note=note,
                       suggest_id=None, suggest_region=None)
        db.log("approve", plot_id, f"{by} -> dashboard #{target['id']}")
        if note:
            notify("done", f"كانت محجوزة في الداشبورد: {_label(plot)}", f"قطعة الداشبورد #{target['id']}",
                   plot_id=plot_id)
        else:
            notify("reserved", f"{'اتحجزت تلقائي' if by == 'auto' else 'اتحجزت'} في الداشبورد: {_label(plot)}",
                   f"قطعة الداشبورد #{target['id']} ({target['plot']})", level="success", plot_id=plot_id,
                   important=by == "auto")
    except dashboard.NotConfigured as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        db.update_plot(plot_id, status="failed", note=str(e))
        db.log("approve_failed", plot_id, f"{by}: {e}")
        notify("review", f"مقدرتش أحدّث الداشبورد: {_label(plot)}", str(e)[:200], level="error", plot_id=plot_id,
               important=by != "manual")
    return _get_plot(plot_id)


async def verify(plot_id: int) -> dict | None:
    """READ-ONLY: if a pending plot is already reserved in the dashboard, tick it off as done.

    Never sends update-status and never marks 'failed': anything unclear leaves the row untouched.
    """
    rows = db.q("SELECT * FROM plots WHERE id=?", (plot_id,))
    if not rows or rows[0]["status"] != "pending" or not dashboard.is_configured():
        return rows[0] if rows else None
    try:
        target, confident, _, _ = await find_target(rows[0], cached=True)
    except Exception as e:
        db.log("verify_error", plot_id, str(e))
        notify("error", "الداشبورد مش بيرد", str(e)[:200], level="error", important=True, throttle="dashboard")
        return rows[0]
    if (target and confident and target["status"] == "reserved"
            and _get_plot(plot_id)["status"] == "pending"):  # the user may have acted meanwhile
        db.update_plot(plot_id, status="done", dashboard_id=target["id"],
                       note="كانت محجوزة في الداشبورد (اتشافت تلقائي)")
        db.log("auto_verified", plot_id, f"dashboard #{target['id']}")
        notify("done", f"كانت محجوزة في الداشبورد: {_label(rows[0])}", f"قطعة الداشبورد #{target['id']}",
               plot_id=plot_id)
    return _get_plot(plot_id)


# ---------- background jobs: one at a time, so catch-up never waits on the dashboard ----------

_jobs: asyncio.Queue = asyncio.Queue()
_queued: dict[int, asyncio.Future] = {}  # plot id -> resolved when its job finishes
_batch: dict | None = None  # progress of the last "verify pending" run, for the panel's progress bar


def enqueue(plot_id: int, kind: str = "verify") -> asyncio.Future:
    """kind: 'new' (approve in auto mode, else verify) or 'verify'. A plot is queued only once."""
    if plot_id not in _queued:
        _queued[plot_id] = asyncio.get_running_loop().create_future()
        _jobs.put_nowait((plot_id, kind))
    return _queued[plot_id]


def enqueue_pending() -> list[asyncio.Future]:
    if not dashboard.is_configured():
        return []
    return [enqueue(r["id"]) for r in db.q("SELECT id FROM plots WHERE status='pending' ORDER BY msg_id")]


async def worker():
    while True:
        plot_id, kind = await _jobs.get()
        try:
            if kind == "new" and db.get_setting("auto_mode") == "1" and dashboard.is_configured():
                if db.q("SELECT 1 FROM plots WHERE id=? AND status='pending'", (plot_id,)):
                    await approve(plot_id, by="auto", cached=True)
            else:
                await verify(plot_id)
        except Exception as e:
            logging.exception("job for plot %s failed", plot_id)
            db.log("error", plot_id, f"job: {e}")
            notify("error", "حصل خطأ وانا بشتغل على قطعة", str(e)[:200], level="error", plot_id=plot_id,
                   important=True, throttle="job")
        finally:
            fut = _queued.pop(plot_id, None)
            if fut and not fut.done():
                fut.set_result(None)
            _jobs.task_done()
        await asyncio.sleep(JOB_DELAY)


_last_compare: dict = {}


async def compare_day(day: str) -> dict:
    """READ-ONLY: the dashboard's own "reserved on `day`" list vs our Telegram rows.

    Every dashboard plot lands in one bucket:
      ok         linked to a Telegram row posted that day
      other_day  linked to a Telegram row from another day (e.g. yesterday evening's, reserved this morning)
      likely     not linked, but a Telegram row handled by hand (no saved link) has the same city and number
      wrong      no Telegram message at all: reserved in the dashboard by someone else, or by mistake
    plus not_reserved: our rows of that day still pending or in review.
    """
    dash = await dashboard.reserved_on(day)
    ours = db.q("SELECT * FROM plots WHERE posted_at LIKE ? AND status NOT IN ('duplicate','rejected')", (day + "%",))
    linked = {r["dashboard_id"]: r for r in db.q("SELECT * FROM plots WHERE dashboard_id IS NOT NULL")}
    unlinked = db.q("SELECT * FROM plots WHERE dashboard_id IS NULL AND status NOT IN ('duplicate','rejected')")
    out = {"day": day, "telegram": len(ours), "dashboard": len(dash), "ok": 0,
           "wrong": [], "likely": [], "other_day": [], "checked_at": db.now()}
    for d in dash:
        r = linked.get(d["id"])
        if r and r["posted_at"].startswith(day):
            out["ok"] += 1
        elif r:
            out["other_day"].append({**d, "our": r})
        elif cand := next((p for p in unlinked if normalize(p["city"]) == normalize(d["city"])
                           and dashboard.same_plot_number(d["plot"], p["plot"], p["block"])), None):
            out["likely"].append({**d, "our": cand})
        else:
            out["wrong"].append(d)
    out["not_reserved"] = [r for r in ours if r["status"] in ("pending", "failed")]
    if day == datetime.now(db.TZ).date().isoformat():  # the header badge shows today's result
        _last_compare.update(day=day, wrong=len(out["wrong"]), checked_at=out["checked_at"])
    return out


def _ar_time(s: str | None) -> str:
    """madinty's "2026-10-28 09:03 AM" -> "9:03 ص" (12-hour clock, like the panel)."""
    try:
        t = datetime.strptime(s or "", "%Y-%m-%d %I:%M %p")
    except ValueError:
        return s or "؟"
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'ص' if t.hour < 12 else 'م'}"


async def compare_and_warn(day: str) -> dict:
    """Compare `day` and warn (once per dashboard plot per day) about reservations with no Telegram message."""
    res = await compare_day(day)
    wrong = {d["id"]: d for d in res["wrong"]}
    for i in sorted(db.new_today("compare_notified", day, wrong)):
        d = wrong[i]
        notify("wrong", f"حجز في مدينتي من غير رسالة تليجرام: {d['city']} – رقم {d['plot']}",
               f"{d['region']} | {(d['area'] or 0):g} م² | قطعة الداشبورد #{d['id']} | اتحجزت {_ar_time(d['reserved_at'])}",
               level="error", important=True)
    return res


async def compare_loop():
    await asyncio.sleep(60)  # first run soon after startup, so the header badge has a number
    while True:
        if dashboard.is_configured():
            try:
                await compare_and_warn(datetime.now(db.TZ).date().isoformat())
            except Exception as e:
                notify("error", "مقدرتش أقارن مع الداشبورد", str(e)[:200], level="error", important=True,
                       throttle="dashboard")
        await asyncio.sleep(COMPARE_EVERY)


async def recheck_loop():
    while True:
        await asyncio.sleep(RECHECK_EVERY)
        enqueue_pending()


async def on_new_plot(plot_id: int):
    """Queue it: auto mode approves, otherwise a read-only check ticks off plots already reserved."""
    enqueue(plot_id, "new")


listener = Listener(on_new_plot=on_new_plot)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(worker()), asyncio.create_task(recheck_loop()), asyncio.create_task(compare_loop())]
    if db.get_setting("listener_enabled") == "1":
        try:
            await listener.start()
        except Exception as e:
            logging.error("listener didn't start: %s", e)
            notify("error", "مقدرتش أبدأ سماع القناة", str(e)[:200], level="error", important=True)
    yield
    for t in tasks:
        t.cancel()
    await listener.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # shell.css / shell.js shared by every page


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/compare")
def compare_page():
    return FileResponse(STATIC / "compare.html")


@app.get("/api/state")
def state():
    today = datetime.now(db.TZ).date().isoformat()
    counts = {r["status"]: r["n"] for r in db.q(
        "SELECT status, COUNT(*) n FROM plots WHERE posted_at LIKE ? GROUP BY status", (today + "%",))}
    return {
        "listener": {"running": listener.running, "error": listener.error,
                     "last_message_at": listener.last_message_at},
        "auto_mode": db.get_setting("auto_mode") == "1",
        "queue": len(_queued),
        "verify_batch": _batch,
        "compare": _last_compare if _last_compare.get("day") == today else None,
        "dashboard_configured": dashboard.is_configured(),
        "dashboard_url": os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
                         + f"/{os.environ.get('DASHBOARD_LOCALE', 'ar')}/dashboard/land_plots",
        "today": today,
        "counts": counts,
        "missing": db.missing_day_counts(today),
        "unparsed": db.unparsed_today(today),
        "tg_channel_id": db.get_setting("tg_channel_id"),
        "notifications_unread": unread_important(),
    }


@app.get("/api/plots")
def plots(status: str | None = None, day: str | None = None, search: str | None = None):
    sql, args = "SELECT * FROM plots WHERE 1=1", []
    if status:
        sql += " AND status=?"; args.append(status)
    if day:
        sql += " AND posted_at LIKE ?"; args.append(day + "%")
    if search:
        sql += " AND (plot=? OR city LIKE ? OR sector LIKE ? OR block LIKE ?)"
        args += [search] + [f"%{search}%"] * 3
    return db.q(sql + " ORDER BY msg_id DESC LIMIT 1000", args)


@app.post("/api/plots/{plot_id}/approve")
async def approve_one(plot_id: int):
    return await approve(plot_id)


@app.post("/api/plots/{plot_id}/link-region")
async def link_region(plot_id: int):
    """Accept the suggested dashboard plot (and its region name, remembered for next time).

    The suggestion was saved when the row was last matched. Matching may have improved since, so look again
    first: if today's suggestion differs, show it and act on nothing.
    """
    plot = _get_plot(plot_id)
    if not plot["suggest_id"]:
        raise HTTPException(400, "مفيش اقتراح للقطعة دي")
    try:
        target, _, c, reason = await find_target(plot)
    except dashboard.NotConfigured as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"مش قادر أوصل للداشبورد: {e}")
    if not c or c["id"] != plot["suggest_id"]:
        if c:
            _mark_unsure(plot, c)
        elif target:
            db.update_plot(plot_id, suggest_id=None, suggest_region=None,
                           note=f"دلوقتي فيه قطعة مطابقة أكيدة #{target['id']} ({target['plot']}). اضغط \"حدّث الداشبورد\"")
        else:
            db.update_plot(plot_id, suggest_id=None, suggest_region=None, note=reason)
        db.log("link_region_stale", plot_id,
               f"saved #{plot['suggest_id']}, now #{(c or target or {}).get('id')}")
        raise HTTPException(409, "الاقتراح اتغيّر بعد تحديث المطابقة. راجع القطعة تاني قبل ما تربط")
    if normalize(c["region"]) != normalize(plot["sector"]):  # area-only differences teach no alias
        db.set_region_alias(plot["city"], plot["sector"], c["region"])
        db.log("link_region", plot_id, f"{plot['sector']} -> {c['region']}")
    return await approve(plot_id, force_id=c["id"])


@app.post("/api/plots/approve-pending")
async def approve_all():
    ids = [r["id"] for r in db.q("SELECT id FROM plots WHERE status='pending' ORDER BY msg_id")]
    results = [await approve(i) for i in ids]
    return {"done": sum(r["status"] == "done" for r in results),
            "failed": sum(r["status"] == "failed" for r in results)}


@app.post("/api/plots/verify-pending")
async def verify_pending():
    """Read-only re-check of every pending row. Waits for the queue so it can report the count."""
    if not dashboard.is_configured():
        raise HTTPException(409, "لازم الربط مع الداشبورد يكون جاهز الأول")
    global _batch
    if _batch and _batch["checked"] < _batch["total"]:
        raise HTTPException(409, "في مراجعة شغالة دلوقتي")
    ids = [r["id"] for r in db.q("SELECT id FROM plots WHERE status='pending' ORDER BY msg_id")]
    futs = [enqueue(i) for i in ids]
    batch = _batch = {"total": len(futs), "checked": 0, "done": 0}

    def tick(_, plot_id):
        batch["checked"] += 1
        batch["done"] += _get_plot(plot_id)["status"] == "done"
    for plot_id, fut in zip(ids, futs):
        fut.add_done_callback(lambda f, i=plot_id: tick(f, i))
    await asyncio.gather(*futs)
    return {"checked": batch["total"], "done": batch["done"]}


@app.get("/api/compare")
async def compare(day: str | None = None):
    if not dashboard.is_configured():
        raise HTTPException(409, "لازم الربط مع الداشبورد يكون جاهز الأول")
    try:
        return await compare_day(day or datetime.now(db.TZ).date().isoformat())
    except Exception as e:
        raise HTTPException(502, f"مش قادر أوصل للداشبورد: {e}")


@app.post("/api/plots/{plot_id}/mark-done")
def mark_done(plot_id: int):
    """Updated by hand in the dashboard — just tick it off here."""
    db.update_plot(plot_id, status="done", note="اتحدّثت يدوي")
    db.log("mark_done", plot_id)
    return {"ok": True}


@app.post("/api/plots/{plot_id}/reject")
def reject(plot_id: int):
    db.update_plot(plot_id, status="rejected")
    db.log("reject", plot_id)
    return {"ok": True}


@app.post("/api/plots/{plot_id}/reset")
def reset(plot_id: int):
    db.update_plot(plot_id, status="pending", note=None)
    db.log("reset", plot_id)
    return {"ok": True}


@app.post("/api/listener/start")
async def start():
    try:
        return {"caught_up": await listener.start()}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/listener/refetch-today")
async def refetch_today():
    """Recover gaps: re-read all of today's channel messages, ignoring last_msg_id."""
    try:
        return {"added": await listener.refetch_today()}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/listener/stop")
async def stop():
    await listener.stop()
    return {"ok": True}


@app.post("/api/auto-mode/{on}")
def auto_mode(on: int):
    if on and not dashboard.is_configured():
        raise HTTPException(409, "لازم الربط مع الداشبورد يكون جاهز الأول")
    db.set_setting("auto_mode", 1 if on else 0)
    db.log("auto_mode", detail=str(on))
    notify("settings", "التحديث التلقائي اشتغل" if on else "التحديث التلقائي اتقفل")
    return {"ok": True}


@app.get("/unparsed")
def unparsed_page():
    return FileResponse(STATIC / "unparsed.html")


@app.get("/api/unparsed")
async def unparsed_list(day: str | None = None):
    """Every message that looked like a reservation but didn't parse, with the reason and the full text.

    While the listener is connected it also reads them from the channel: older rows stored only 200
    characters, and the channel has the post/edit times. Read-only towards Telegram."""
    items = db.unparsed(day)
    try:
        live = await listener.fetch([u["msg_id"] for u in items])
    except Exception as e:
        logging.getLogger("unparsed").warning("reading from the channel failed: %s", e)
        live = {}
    for u in items:
        m = live.get(u["msg_id"])
        if m and m.message and m.message != u["text"]:
            u["text"] = m.message
            db.set_unparsed_text(u["msg_id"], m.message)
        u["posted_at"] = m.date.astimezone(db.TZ).replace(tzinfo=None).isoformat() if m else None
        u["edited_at"] = m.edit_date.astimezone(db.TZ).replace(tzinfo=None).isoformat() if m and m.edit_date else None
        u["deleted"] = bool(live) and m is None  # connected, but the channel no longer has it
        u["reason"] = why_unparsed(u["text"])
        u["counter"] = (c := re.search(r"اجمالي\s*اليوم\s*:?\s*(\d+)", u["text"].translate(_AR_NORMALIZE))) and int(c[1])
    return {"items": items, "live": bool(live)}


@app.post("/api/unparsed/{msg_id}/{action}")
def unparsed_action(msg_id: int, action: str):
    if action not in ("dismiss", "restore"):
        raise HTTPException(404)
    db.dismiss_unparsed(msg_id, hide=action == "dismiss")
    return {"ok": True}


@app.get("/notifications")
def notifications_page():
    return FileResponse(STATIC / "notifications.html")


@app.get("/api/notifications")
def notifications(filter: str = "all", limit: int = 300):
    where = {"important": "n.important=1", "unread": "n.read=0"}.get(filter, "1=1")
    return {
        "items": db.q(f"""SELECT n.*, p.plot AS plot_no, p.status AS plot_status FROM notifications n
                          LEFT JOIN plots p ON p.id = n.plot_id WHERE {where} ORDER BY n.id DESC LIMIT ?""",
                      (min(limit, 1000),)),
        "unread": db.q("SELECT COUNT(*) n FROM notifications WHERE read=0")[0]["n"],
        "unread_important": unread_important(),
        "windows": windows_enabled(),
    }


@app.post("/api/notifications/read")
def notifications_read(up_to: int):
    db.x("UPDATE notifications SET read=1 WHERE id<=? AND read=0", (up_to,))
    return {"ok": True}


class PanelMessage(BaseModel):
    text: str
    level: str = "info"


@app.post("/api/notifications/panel")
def notifications_panel(m: PanelMessage):
    """Toasts shown in the panel, so the notifications page has every message the user saw."""
    notify("panel", m.text[:300], level=m.level if m.level in ("info", "success", "warning", "error") else "info")
    return {"ok": True}


@app.post("/api/notifications/windows/{on}")
def notifications_windows(on: int):
    db.set_setting("win_notify", 1 if on else 0)
    return {"ok": True}


@app.post("/api/notifications/test")
async def notifications_test():
    ok = await asyncio.to_thread(show_windows_toast, "🔔 تجربة إشعارات اللوحة", "لو شايف الرسالة دي يبقى الإشعارات شغالة")
    notify("test", "إشعار تجربة" + (" اتبعت لويندوز" if ok else ": ويندوز رفض الإشعار"), level="success" if ok else "error")
    return {"ok": ok}


@app.get("/api/log")
def get_log():
    return db.q("SELECT * FROM log ORDER BY id DESC LIMIT 200")
