import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from . import dashboard, db  # noqa: E402
from .listener import Listener  # noqa: E402

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent / "static"


def _get_plot(plot_id: int) -> dict:
    rows = db.q("SELECT * FROM plots WHERE id=?", (plot_id,))
    if not rows:
        raise HTTPException(404)
    return rows[0]


SEARCH_TTL = 60      # seconds a search result is reused within a batch
JOB_DELAY = 0.3      # pause between background jobs so we don't hammer the dashboard
RECHECK_EVERY = 300  # re-verify rows still pending (someone may reserve them by hand later)
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
            db.update_plot(plot_id, status="failed", suggest_id=c["id"], suggest_region=c["region"],
                           note=f"المنطقة في الداشبورد اسمها \"{c['region']}\" ({c['area']}م²). "
                                f"لو هي نفسها اضغط \"اربط وحدّث\"")
            db.log("approve_unsure", plot_id, f"{by}: suggest #{c['id']}")
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
    except dashboard.NotConfigured as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        db.update_plot(plot_id, status="failed", note=str(e))
        db.log("approve_failed", plot_id, f"{by}: {e}")
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
        return rows[0]
    if (target and confident and target["status"] == "reserved"
            and _get_plot(plot_id)["status"] == "pending"):  # the user may have acted meanwhile
        db.update_plot(plot_id, status="done", dashboard_id=target["id"],
                       note="كانت محجوزة في الداشبورد (اتشافت تلقائي)")
        db.log("auto_verified", plot_id, f"dashboard #{target['id']}")
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
        finally:
            fut = _queued.pop(plot_id, None)
            if fut and not fut.done():
                fut.set_result(None)
            _jobs.task_done()
        await asyncio.sleep(JOB_DELAY)


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
    tasks = [asyncio.create_task(worker()), asyncio.create_task(recheck_loop())]
    if db.get_setting("listener_enabled") == "1":
        try:
            await listener.start()
        except Exception as e:
            logging.error("listener didn't start: %s", e)
    yield
    for t in tasks:
        t.cancel()
    await listener.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


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
        "dashboard_configured": dashboard.is_configured(),
        "dashboard_url": os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
                         + f"/{os.environ.get('DASHBOARD_LOCALE', 'ar')}/dashboard/land_plots",
        "today": today,
        "counts": counts,
        "missing": db.missing_day_counts(today),
        "unparsed": db.unparsed_today(today),
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
    """Accept the suggested dashboard region for this Telegram region (remembered for next time)."""
    plot = _get_plot(plot_id)
    if not plot["suggest_id"]:
        raise HTTPException(400, "مفيش اقتراح للقطعة دي")
    db.set_region_alias(plot["city"], plot["sector"], plot["suggest_region"])
    db.log("link_region", plot_id, f"{plot['sector']} -> {plot['suggest_region']}")
    return await approve(plot_id, force_id=plot["suggest_id"])


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
    return {"ok": True}


@app.get("/api/log")
def get_log():
    return db.q("SELECT * FROM log ORDER BY id DESC LIMIT 200")
