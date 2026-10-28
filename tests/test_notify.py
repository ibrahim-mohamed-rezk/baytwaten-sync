"""Run: python -m tests.test_notify

The notification store, Windows toast routing, throttling, the API and the event hooks. Uses a throwaway
SQLite file and a recorder in place of the real Windows toast, so nothing pops up on screen.
"""
import asyncio
import os
import tempfile
import threading
from pathlib import Path

from app.console import utf8_console

utf8_console()
os.environ.update(
    SYNC_DB_PATH=str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
    DASHBOARD_BASE_URL="http://fake", DASHBOARD_LOGIN_PATH="/login", DASHBOARD_LOCALE="ar",
    DASHBOARD_EMAIL="any", DASHBOARD_PASSWORD="pw", DASHBOARD_COOKIE="",
)
import httpx  # noqa: E402

from app import dashboard, db, main, notify as N  # noqa: E402
from tests import fake_dashboard as fake  # noqa: E402
from tests.test_listener import FakeMessage, RESERVATIONS, make_listener  # noqa: E402

shown: list[tuple[str, str]] = []
_done = threading.Event()


def fake_toast(title, body=""):
    shown.append((title, body))
    _done.set()
    return True


N.show_windows_toast = fake_toast
N.windows_enabled = lambda: db.get_setting("win_notify", "1") == "1"  # same rule, minus the platform check


def wait_toast():
    assert _done.wait(2), "no Windows toast was sent"
    _done.clear()


def rows(kind=None) -> list[dict]:
    return db.q("SELECT * FROM notifications" + (" WHERE kind=?" if kind else "") + " ORDER BY id", (kind,) if kind else ())


async def test_notify_stores_and_routes_important():
    db.set_setting("win_notify", 1)
    shown.clear()
    N.notify("new_plot", "قطعة جديدة", "تفاصيل")
    assert rows()[-1]["important"] == 0 and shown == [], "info stays on the page only"
    N.notify("gap", "رسايل ناقصة", "67", level="warning", important=True)
    wait_toast()
    assert shown == [("رسايل ناقصة", "67")] and rows()[-1]["level"] == "warning"

    db.set_setting("win_notify", 0)
    N.notify("gap", "تاني", important=True)
    await asyncio.sleep(0.1)
    assert len(shown) == 1 and rows()[-1]["title"] == "تاني", "switched off: still listed, not shown in Windows"
    db.set_setting("win_notify", 1)


async def test_throttle():
    before = len(rows())
    assert N.notify("error", "الداشبورد مش بيرد", throttle="t", every=60)
    assert N.notify("error", "الداشبورد مش بيرد", throttle="t", every=60) is None
    assert len(rows()) == before + 1


async def test_api_lists_filters_and_marks_read():
    N.notify("review", "مهمة", important=True)
    wait_toast()
    main.notifications_panel(main.PanelMessage(text="✅ اتنسخ", level="success"))
    data = main.notifications("all")
    assert data["items"][0]["kind"] == "panel" and data["unread"] == len(rows())
    assert all(n["important"] for n in main.notifications("important")["items"])
    main.notifications_read(up_to=data["items"][0]["id"])
    assert main.notifications("unread")["items"] == [] and main.state()["notifications_unread"] == 0


async def test_events_from_listener_and_approve():
    db.set_setting("win_notify", 1)
    shown.clear()
    lst = make_listener([])
    m = FakeMessage(900, RESERVATIONS[0].replace("رقم القطعة: 172", "رقم القطعة: 5172"))
    pid = await lst.save_message(m)
    new = rows("new_plot")[-1]
    assert new["plot_id"] == pid and "5172" in new["title"] and not new["important"]

    await lst.save_message(FakeMessage(901, "🏗 تم حجز قطعة\nشكل غريب"))
    wait_toast()
    assert rows("unparsed") and shown[-1][0] == "رسالة حجز مااتقرتش"
    await lst.save_message(FakeMessage(901, "🏗 تم حجز قطعة\nشكل غريب"))
    assert len(rows("unparsed")) == 1, "resync re-reading it must not notify again"

    # auto mode reserving in the dashboard -> important; a manual reserve -> page only
    dashboard._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app), base_url="http://fake",
                                          follow_redirects=True)
    p = db.insert_plot(902, db.now(), dict(day_count=None, city="العبور الجديدة", sector="الحى 24 -منطقة TJ",
                       block="", plot="271", area=500, features=None, down_payment=None, project_reserved=None,
                       project_total=None), "k902", "raw")
    shown.clear()
    r = await main.approve(p, by="auto")
    assert r["status"] == "done"
    wait_toast()
    res = rows("reserved")[-1]
    assert res["important"] and "تلقائي" in res["title"] and shown[-1][0] == res["title"]


async def test_gap_warning_once_per_day():
    assert db.new_today("gap_notified", "2026-10-28", {67}) == {"67"}
    assert db.new_today("gap_notified", "2026-10-28", {67}) == set(), "a restart must not repeat it"
    assert db.new_today("gap_notified", "2026-10-28", {67, 70}) == {"70"}
    assert db.new_today("gap_notified", "2026-10-29", {67}) == {"67"}, "new day, new warnings"


async def run():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            await fn(); print("✓", name)


if __name__ == "__main__":
    asyncio.run(run())
