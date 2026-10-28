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
db.set_setting("win_notify", 0)  # important events would otherwise pop real Windows notifications

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


async def test_link_region_rechecks_saved_suggestion():
    from fastapi import HTTPException
    use_dashboard(httpx.ASGITransport(app=fake.app))
    posts = len(fake.POSTS)
    # a suggestion saved by older matching rules: plot 777 now points at 8909, not 8908
    p = db.q("SELECT id FROM plots WHERE plot='777'")[0]["id"]
    db.update_plot(p, suggest_id="8908", suggest_region="الحى 24 -منطقة TJ", note="old")
    try:
        await main.link_region(p)
        raise AssertionError("a stale suggestion must not be acted on")
    except HTTPException as e:
        assert e.status_code == 409
    r = row(p)
    assert (r["status"], r["suggest_id"]) == ("failed", "8909") and "المساحة" in r["note"], r
    assert len(fake.POSTS) == posts and not db.q("SELECT 1 FROM region_aliases")

    # a current suggestion still links: plot 612, region named differently -> alias learned, row done
    m = db.q("SELECT id FROM plots WHERE plot='612'")[0]["id"]
    r = await main.link_region(m)
    assert (r["status"], r["dashboard_id"]) == ("done", "8908"), r
    assert db.get_region_alias("العبور الجديدة", "الحى العاشر") == "الحى 24 -منطقة TJ"
    assert len(fake.POSTS) == posts, "8908 was already reserved, nothing to send"


async def test_search_reads_every_page():
    """q is a substring search, so a short number can exceed one page (real q=10: 655 rows > 500).
    السادات C10 sat on page 2 and came back "not found"."""
    use_dashboard(httpx.ASGITransport(app=fake.app))
    old, dashboard.PER_PAGE = dashboard.PER_PAGE, 1
    try:
        rows = await dashboard.search("38")  # 7001 (العبور) on page 1, 7002 (السادات) on page 2
        assert sorted(r["id"] for r in rows) == ["7001", "7002"], rows
        tg = {"city": "السادات", "sector": "المحور المركزى", "plot": "38", "area": 500}
        assert [c["id"] for c in dashboard.match(tg, rows)[0]] == ["7002"]
    finally:
        dashboard.PER_PAGE = old


async def test_compare_day():
    """The dashboard's own "reserved on D" list vs Telegram, one plot per bucket. Read-only."""
    use_dashboard(httpx.ASGITransport(app=fake.app))
    D, city, region = "2030-01-15", "العبور الجديدة", "الحى 24 -منطقة TJ"
    for i, plot, at in [("9001", "101", D), ("9002", "102", D), ("9003", "103", D), ("9004", "104", D),
                        ("9005", "105", "2030-01-14")]:
        fake.PLOTS[i] = dict(plot=plot, phase="1", city=city, region=region, nb="", area=500, status="reserved",
                             at=f"{at} 10:00 AM")

    def tg(plot, posted, **fields):
        r = dict(day_count=None, city=city, sector=region, block="", plot=plot, area=500, features=None,
                 down_payment=None, project_reserved=None, project_total=None)
        pid = db.insert_plot(next(_msg_ids), f"{posted}T09:00:00", r, plot_key(r), "test")
        db.update_plot(pid, **fields)
        return pid
    ok = tg("101", D, status="done", dashboard_id="9001")
    other = tg("102", "2030-01-14", status="done", dashboard_id="9002")   # yesterday's message
    manual = tg("103", D, status="done", note="اتحدّثت يدوي")                # ticked by hand, no link
    waiting = tg("106", D)                                                  # still pending
    posts = len(fake.POSTS)

    r = await main.compare_day(D)
    assert (r["telegram"], r["dashboard"], r["ok"]) == (3, 4, 1), r  # 102 was posted the day before
    assert [d["id"] for d in r["wrong"]] == ["9004"], r["wrong"]
    assert [(d["id"], d["our"]["id"]) for d in r["likely"]] == [("9003", manual)]
    assert [(d["id"], d["our"]["id"]) for d in r["other_day"]] == [("9002", other)]
    assert [p["id"] for p in r["not_reserved"]] == [waiting] and ok
    assert r["wrong"][0]["reserved_at"] == f"{D} 10:00 AM"
    assert len(fake.POSTS) == posts, "comparing is read-only"

    # the background check warns once per wrong plot, not on every run
    await main.compare_and_warn(D)
    await main.compare_and_warn(D)
    warned = db.q("SELECT * FROM notifications WHERE kind='wrong'")
    assert len(warned) == 1 and "104" in warned[0]["title"] and warned[0]["important"], warned
    assert "10:00 ص" in warned[0]["body"], warned[0]["body"]

    # the header badge carries today's result only
    assert main.state()["compare"] is None


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
