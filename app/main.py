import logging
import os
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


async def approve(plot_id: int, by: str = "manual", force_id: str | None = None) -> dict:
    """Find the plot in the dashboard and mark it reserved.

    Only acts when the match is certain; anything else becomes 'failed' with a note
    (and a suggested dashboard plot when there's exactly one candidate in another region).
    """
    plot = _get_plot(plot_id)
    if plot["status"] == "done":
        return plot
    try:
        found = await dashboard.search(plot["plot"])
        if force_id:
            target = next((c for c in found if c["id"] == force_id), None)
            if not target:
                raise LookupError(f"القطعة #{force_id} مش موجودة في الداشبورد")
        else:
            pool, confident = dashboard.match(
                plot, found, db.get_region_alias(plot["city"], plot["sector"]))
            if not pool:
                raise LookupError(f"مفيش قطعة رقم {plot['plot']} في {plot['city']} على الداشبورد")
            if len(pool) > 1:
                raise LookupError("لقيت أكتر من قطعة مطابقة: " + "، ".join(
                    f"#{c['id']} {c['region']} {c['area']}م²" for c in pool))
            if not confident:
                c = pool[0]
                db.update_plot(plot_id, status="failed", suggest_id=c["id"], suggest_region=c["region"],
                               note=f"المنطقة في الداشبورد اسمها \"{c['region']}\" ({c['area']}م²). "
                                    f"لو هي نفسها اضغط \"اربط وحدّث\"")
                db.log("approve_unsure", plot_id, f"{by}: suggest #{c['id']}")
                return _get_plot(plot_id)
            target = pool[0]

        if target["status"] == "reserved":
            note = "كانت محجوزة أصلاً في الداشبورد"
        elif target["status"] != "available":
            raise LookupError(f"حالة القطعة في الداشبورد \"{target['status']}\"، مش متاحة")
        else:
            await dashboard.reserve(target["id"])
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


async def on_new_plot(plot_id: int):
    """Auto mode: approve right away; anything not 100% clear ends up as 'failed' for review."""
    if db.get_setting("auto_mode") == "1" and dashboard.is_configured():
        if db.q("SELECT 1 FROM plots WHERE id=? AND status='pending'", (plot_id,)):
            await approve(plot_id, by="auto")


listener = Listener(on_new_plot=on_new_plot)


@asynccontextmanager
async def lifespan(app):
    if db.get_setting("listener_enabled") == "1":
        try:
            await listener.start()
        except Exception as e:
            logging.error("listener didn't start: %s", e)
    yield
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
        "dashboard_configured": dashboard.is_configured(),
        "dashboard_url": os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
                         + f"/{os.environ.get('DASHBOARD_LOCALE', 'ar')}/dashboard/land_plots",
        "today": today,
        "counts": counts,
        "missing": db.missing_day_counts(today),
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
