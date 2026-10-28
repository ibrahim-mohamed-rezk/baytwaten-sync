"""Run: python -m tests.test_listener

Resync / refetch / unparsed logging against a fake Telethon client and a throwaway SQLite file.
No Telegram account, no network, data.sqlite3 untouched.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.console import utf8_console

utf8_console()
os.environ["SYNC_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.sqlite3")
from app import db, listener as L  # noqa: E402
from tests.test_parser import messages  # noqa: E402

RESERVATIONS = [m for m in messages() if "تم حجز قطعة" in m]  # skips the hourly summary
BROKEN = "🏗 تم حجز قطعة (إجمالي اليوم : 99)\n🏙 مدينة\n   رقم القطعه 5"  # no "• رقم القطعة:" field


class FakeMessage:
    def __init__(self, msg_id, text, minutes_ago=0):
        self.id, self.message = msg_id, text
        self.date = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


class FakeClient:
    """iter_messages() the way Telethon does it: newest first, or oldest first with reverse=True;
    min_id/offset_date bound the range, limit caps it."""
    def __init__(self, msgs):
        self.msgs = msgs

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return True

    async def iter_messages(self, channel, limit=None, min_id=0, reverse=False, offset_date=None):
        ms = sorted((m for m in self.msgs if m.id > min_id), key=lambda m: m.id, reverse=not reverse)
        if offset_date and reverse:
            ms = [m for m in ms if m.date >= offset_date]
        for m in ms[:limit]:
            yield m


def make_listener(msgs) -> L.Listener:
    lst = L.Listener()
    lst.client, lst.channel = FakeClient(msgs), object()
    return lst


def saved(msg_id) -> bool:
    return bool(db.q("SELECT 1 FROM plots WHERE msg_id=?", (msg_id,)))


async def test_resync_recovers_older_than_last_msg_id():
    msgs = [FakeMessage(100 + i, t, minutes_ago=30 - i) for i, t in enumerate(RESERVATIONS)]
    lst = make_listener(msgs)
    missed = msgs[2]
    for m in msgs:
        if m is not missed:
            await lst.save_message(m)  # push updates for everything but one
    assert int(db.get_setting("last_msg_id")) > missed.id and not saved(missed.id)

    assert await lst.catch_up() == 0, "catch-up starts after last_msg_id, so it can't see the missed one"
    assert await lst.resync() == 1 and saved(missed.id)
    assert db.q("SELECT detail FROM log WHERE action='resync_recovered'")[0]["detail"] == "recovered 1"
    assert await lst.resync() == 0, "resync is idempotent"


async def test_refetch_today_reaches_past_the_resync_window():
    first = FakeMessage(200, RESERVATIONS[0].replace("رقم القطعة: 172", "رقم القطعة: 9172"))
    msgs = [first] + [FakeMessage(201 + i, t) for i, t in enumerate(RESERVATIONS)]
    lst = make_listener(msgs)
    old_limit, L.RESYNC_LIMIT = L.RESYNC_LIMIT, 3
    try:
        await lst.resync()
        assert not saved(200), "outside the resync window"
        assert await lst.refetch_today() >= 1 and saved(200)
    finally:
        L.RESYNC_LIMIT = old_limit


async def test_unparsed_is_logged_once_and_clears_when_saved():
    today = datetime.now(db.TZ).date().isoformat()
    broken = FakeMessage(300, BROKEN)
    lst = make_listener([broken])
    await lst.resync(); await lst.resync()
    [u] = db.unparsed_today(today)
    assert u["msg_id"] == 300 and u["text"] == BROKEN[:200]
    assert len(db.q("SELECT 1 FROM log WHERE action='unparsed'")) == 1
    # the channel edits it into a parseable message -> the next resync saves it and the alert clears
    broken.message = RESERVATIONS[0].replace("رقم القطعة: 172", "رقم القطعة: 8172")
    assert await lst.resync() == 1 and db.unparsed_today(today) == []


async def test_insert_plot_dedupes_by_msg_id():
    lst = make_listener([])
    m = FakeMessage(400, RESERVATIONS[1].replace("رقم القطعة: 38", "رقم القطعة: 7038"))
    ids = await asyncio.gather(lst.save_message(m), lst.save_message(m))
    assert ids.count(None) == 1 and len(db.q("SELECT 1 FROM plots WHERE msg_id=400")) == 1, ids


async def test_gap_triggers_resync():
    lst = make_listener([])
    calls = []

    async def fake_resync():
        lst._last_resync = L.monotonic()
        calls.append(1)
        return 0
    lst.resync = fake_resync
    old = L.TICK, L.RESYNC_EVERY, L.GAP_RESYNC_EVERY
    L.TICK, L.RESYNC_EVERY, L.GAP_RESYNC_EVERY = 0.01, 3600, 0.05
    db.missing_day_counts, real = (lambda day: [67]), db.missing_day_counts
    try:
        task = asyncio.create_task(lst._resync_loop())
        await asyncio.sleep(0.03)
        assert calls == [1], "a gap resyncs right away"
        await asyncio.sleep(0.02)
        assert calls == [1], "but at most once per GAP_RESYNC_EVERY"
        db.missing_day_counts = lambda day: []
        await asyncio.sleep(0.1)
        assert calls == [1], "no gap -> only the periodic RESYNC_EVERY"
        task.cancel()
    finally:
        L.TICK, L.RESYNC_EVERY, L.GAP_RESYNC_EVERY = old
        db.missing_day_counts = real


async def run():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            await fn(); print("✓", name)


if __name__ == "__main__":
    asyncio.run(run())
