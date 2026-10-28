"""Run: python -m tests.test_verify

The background verify flow against tests/fake_dashboard.py, served in-process (no port, no network)
with a throwaway SQLite file, so data.sqlite3 and the real .env dashboard are never touched.
"""
import asyncio
import itertools
import os
import tempfile
from pathlib import Path

from app.console import utf8_console

utf8_console()
os.environ.update(
    SYNC_DB_PATH=str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
    DASHBOARD_BASE_URL="http://fake", DASHBOARD_LOGIN_PATH="/login", DASHBOARD_LOCALE="ar",
    DASHBOARD_EMAIL="any", DASHBOARD_PASSWORD="pw", DASHBOARD_COOKIE="", DASHBOARD_UPDATE_METHOD="POST",
)
import httpx  # noqa: E402

from app import dashboard, db, main  # noqa: E402
from app.parser import plot_key  # noqa: E402
from tests import fake_dashboard as fake  # noqa: E402

AUTO_NOTE = "كانت محجوزة في الداشبورد (اتشافت تلقائي)"
_msg_ids = itertools.count(1)


def use_dashboard(transport: httpx.AsyncBaseTransport):
    dashboard._client = httpx.AsyncClient(transport=transport, base_url="http://fake", follow_redirects=True)
    dashboard._csrf = None
    main._search_cache.clear()


def add_plot(city, sector, plot, area, block="") -> int:
    r = dict(day_count=None, city=city, sector=sector, block=block, plot=plot, area=area, features=None,
             down_payment=None, project_reserved=None, project_total=None)
    return db.insert_plot(next(_msg_ids), db.now(), r, plot_key(r), "test")


def row(plot_id) -> dict:
    return db.q("SELECT * FROM plots WHERE id=?", (plot_id,))[0]


async def drain():
    await asyncio.gather(*list(main._queued.values()))


async def test_verify_is_read_only():
    use_dashboard(httpx.ASGITransport(app=fake.app))
    reserved = add_plot("السادات", "المحور المركزى", "38", 500)
    available = add_plot("القاهرة الجديدة", "الامتداد الجنوبى بالقرب من ديستركت 5", "163", 594)
    ambiguous = add_plot("العبور الجديدة", "الحى الخامس", "500", 400)
    mismatch = add_plot("العبور الجديدة", "الحى العاشر", "612", 500)
    before = {k: p["status"] for k, p in fake.PLOTS.items()}

    for i in (reserved, available, ambiguous, mismatch):
        await main.on_new_plot(i)  # auto mode is off -> verify
    assert len(main._queued) == 4, "on_new_plot must only enqueue, never wait on the dashboard"
    await drain()

    r = row(reserved)
    assert (r["status"], r["dashboard_id"], r["note"]) == ("done", "7002", AUTO_NOTE), r
    for i in (available, ambiguous, mismatch):
        r = row(i)
        assert (r["status"], r["note"], r["suggest_id"]) == ("pending", None, None), r
    assert fake.POSTS == [], "verify must never send update-status"
    assert {k: p["status"] for k, p in fake.PLOTS.items()} == before
    assert db.q("SELECT 1 FROM log WHERE action='auto_verified' AND plot_id=?", (reserved,))


async def test_unreachable_and_unconfigured():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)
    use_dashboard(httpx.MockTransport(refuse))
    p = add_plot("السادات", "المحور المركزى", "39", 500)
    await main.verify(p)
    assert row(p)["status"] == "pending" and row(p)["note"] is None
    assert db.q("SELECT 1 FROM log WHERE action='verify_error' AND plot_id=?", (p,))

    os.environ["DASHBOARD_BASE_URL"] = ""
    try:
        await main.verify(p)  # skipped silently
        assert row(p)["status"] == "pending"
        assert main.enqueue_pending() == []
    finally:
        os.environ["DASHBOARD_BASE_URL"] = "http://fake"


async def test_verify_pending_endpoint():
    use_dashboard(httpx.ASGITransport(app=fake.app))
    # reserved by hand in the dashboard after the message arrived
    p = add_plot("العبور الجديدة", "الحى 24 -منطقة TJ", "271", 500)
    assert (await main.verify_pending())["done"] == 0
    fake.PLOTS["8905"]["status"] = "reserved"
    main._search_cache.clear()
    r = await main.verify_pending()
    assert r["done"] == 1 and r["checked"] >= 1, r
    assert main._batch == {"total": r["checked"], "checked": r["checked"], "done": 1}, main._batch
    assert row(p)["status"] == "done" and fake.POSTS == []


async def test_approve_unchanged():
    """approve() on top of find_target still reserves, and still flags region mismatches."""
    use_dashboard(httpx.ASGITransport(app=fake.app))
    available = db.q("SELECT id FROM plots WHERE plot='163'")[0]["id"]
    mismatch = db.q("SELECT id FROM plots WHERE plot='612'")[0]["id"]
    assert (await main.approve(available))["status"] == "done"
    assert fake.POSTS == ["9414"] and fake.PLOTS["9414"]["status"] == "reserved"
    r = await main.approve(mismatch)
    assert (r["status"], r["suggest_id"]) == ("failed", "8908"), r
    # right region and number, but Telegram says 685 m² and the dashboard 583.3 -> suggest, don't reserve
    p = add_plot("العبور الجديدة", "الحى 24 -منطقة TJ", "777", 685)
    r = await main.approve(p)
    assert (r["status"], r["suggest_id"]) == ("failed", "8909") and "المساحة" in r["note"], r
    assert "8909" not in fake.POSTS and fake.PLOTS["8909"]["status"] == "available"


async def run():
    main.JOB_DELAY = 0
    worker = asyncio.create_task(main.worker())
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                await fn(); print("✓", name)
    finally:
        worker.cancel()


if __name__ == "__main__":
    asyncio.run(run())
