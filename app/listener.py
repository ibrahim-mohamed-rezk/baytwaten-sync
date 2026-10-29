"""Reads the Telegram channel with a member account (the channel isn't ours, so no bot)."""
import asyncio
import logging
import os
from datetime import datetime, time, timezone
from time import monotonic

from telethon import TelegramClient, events
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready

from . import db
from .notify import notify
from .parser import parse_reservation, plot_key

log = logging.getLogger("listener")
RESYNC_EVERY = 120     # seconds between periodic resyncs of the latest messages
RESYNC_LIMIT = 100     # how many recent channel messages a resync re-reads
GAP_RESYNC_EVERY = 60  # a gap in the daily counter triggers a resync at most this often
TICK = 15              # how often the resync loop wakes up to check
# plot fields an edit may change on a pending row
EDIT_FIELDS = ("city", "sector", "block", "plot", "area", "features", "down_payment")
SESSION = os.path.join(os.path.dirname(__file__), "..", "telegram")


def make_client() -> TelegramClient:
    if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
        raise RuntimeError("TG_API_ID و TG_API_HASH مش متظبطين في ملف .env")
    return TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], catch_up=True)


async def resolve_channel(client: TelegramClient, ref: str):
    """Accepts a private invite link (t.me/+hash), @username or numeric id."""
    if "/+" in ref or "joinchat/" in ref:
        invite_hash = ref.rstrip("/").split("/")[-1].lstrip("+")
        invite = await client(CheckChatInviteRequest(invite_hash))
        if not isinstance(invite, ChatInviteAlready):
            raise RuntimeError("الحساب ده مش عضو في القناة. ادخلها من التليجرام الأول.")
        return invite.chat
    return await client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)


def _today_utc() -> datetime:
    return datetime.combine(datetime.now(db.TZ).date(), time.min, db.TZ).astimezone(timezone.utc)


class Listener:
    def __init__(self, on_new_plot=None):
        self.client: TelegramClient | None = None
        self.channel = None
        self.running = False
        self.error: str | None = None
        self.last_message_at: str | None = None
        self.on_new_plot = on_new_plot  # async callback(plot_id), used for auto mode
        self._lock = asyncio.Lock()
        self._resync_task: asyncio.Task | None = None
        self._last_resync = 0.0

    async def _ensure_connected(self):
        if self.client is None:
            self.client = make_client()
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("لازم تسجل دخول التليجرام الأول: شغّل python login.py")
        if self.channel is None:
            self.channel = await resolve_channel(self.client, os.environ["TG_CHANNEL"])
            db.set_setting("tg_channel_id", self.channel.id)  # for "open in Telegram" links, also while stopped

    async def save_message(self, msg) -> int | None:
        if not msg.message:
            return None
        r = parse_reservation(msg.message)
        if not r:
            if "تم حجز" in msg.message and db.log_unparsed(msg.id, msg.message):  # never drop it silently
                notify("unparsed", "رسالة حجز مااتقرتش", msg.message[:150], level="warning", important=True)
            return None
        posted = msg.date.astimezone(db.TZ).replace(tzinfo=None).isoformat(timespec="seconds")
        plot_id = db.insert_plot(msg.id, posted, r, plot_key(r), msg.message)
        if msg.id > int(db.get_setting("last_msg_id", 0)):
            db.set_setting("last_msg_id", msg.id)
        if plot_id:
            self.last_message_at = posted
            notify("new_plot", f"قطعة جديدة: {r['city']} – رقم {r['plot']}",
                   " | ".join(filter(None, (r["sector"], r["block"], r["area"] and f"{r['area']:g} م²"))), plot_id=plot_id)
            if self.on_new_plot:
                await self.on_new_plot(plot_id)
        return plot_id

    async def save_edit(self, msg) -> str:
        """The channel edited a message. Returns what we did: new | updated | unchanged | kept | ignored.

        Not saved before -> save it now (it may only parse after the edit).
        Saved and still pending -> take the new plot fields and queue it again. Any other status -> keep the
        row as it is (it may already be reserved in the dashboard) and just log the edit for review.
        """
        rows = db.q("SELECT * FROM plots WHERE msg_id=?", (msg.id,))
        if not rows:
            return "new" if await self.save_message(msg) else "ignored"
        row, r = rows[0], parse_reservation(msg.message or "")
        if not r:
            db.log("edit_unparsed", row["id"], f"msg {msg.id}: {(msg.message or '')[:200]}")
            notify("edit", f"رسالة اتعدّلت ومبقتش تتقري: رقم {row['plot']}", (msg.message or "")[:150],
                   level="warning", plot_id=row["id"], important=True)
            return "ignored"
        changes = {k: r[k] for k in EDIT_FIELDS if r[k] != row[k]}
        if not changes:
            return "unchanged"
        detail = "; ".join(f"{k}: {row[k]} -> {v}" for k, v in changes.items())
        if row["status"] != "pending":
            db.log("edit_ignored", row["id"], f"status {row['status']}: {detail}")
            notify("edit", f"القناة عدّلت قطعة اتعاملت معاها: {row['city']} – رقم {row['plot']}", detail,
                   level="warning", plot_id=row["id"], important=True)
            return "kept"
        db.update_plot(row["id"], **changes, day_count=r["day_count"], plot_key=plot_key(r), raw=msg.message)
        db.log("edited", row["id"], detail)
        notify("edit", f"القناة عدّلت رسالة: {row['city']} – رقم {row['plot']}", detail, plot_id=row["id"])
        if self.on_new_plot:
            await self.on_new_plot(row["id"])
        return "updated"

    async def _on_message(self, event):
        try:
            await self.save_message(event.message)
        except Exception as e:  # never let one bad message kill the listener
            log.exception("failed to save message %s", event.message.id)
            db.log("error", detail=f"msg {event.message.id}: {e}")

    async def _on_edit(self, event):
        try:
            await self.save_edit(event.message)
        except Exception as e:
            log.exception("failed to apply edit of message %s", event.message.id)
            db.log("error", detail=f"edit of msg {event.message.id}: {e}")

    async def _save_all(self, messages) -> int:
        """Save messages (a list or Telethon's async iterator), oldest first. Returns how many were new."""
        if hasattr(messages, "__aiter__"):
            messages = [m async for m in messages]
        added = 0
        for msg in messages:
            if await self.save_message(msg):
                added += 1
        return added

    async def catch_up(self) -> int:
        """Fetch what was posted while we were stopped (or today's messages on first run)."""
        await self._ensure_connected()
        last_id = int(db.get_setting("last_msg_id", 0))
        # oldest first so the daily counter and "last_msg_id" advance in order
        return await self._save_all(self.client.iter_messages(
            self.channel, min_id=last_id, reverse=True, offset_date=None if last_id else _today_utc()))

    async def refetch_today(self) -> int:
        """Re-read ALL of today's messages, ignoring last_msg_id. Safe: inserts are deduped by msg_id."""
        await self._ensure_connected()
        added = await self._save_all(self.client.iter_messages(self.channel, reverse=True, offset_date=_today_utc()))
        db.log("refetch_today", detail=f"recovered {added}")
        notify("recovered", f"إعادة قراية رسايل النهارده: رجعت {added} رسالة", level="success" if added else "info")
        return added

    async def resync(self) -> int:
        """Re-read the latest RESYNC_LIMIT messages. Push updates can be missed (short disconnect, sleep),
        and a missed message can be older than last_msg_id, so this goes by recent window, not min_id."""
        self._last_resync = monotonic()
        await self._ensure_connected()
        recent = [m async for m in self.client.iter_messages(self.channel, limit=RESYNC_LIMIT)]  # newest first
        added = await self._save_all(recent[::-1])
        if added:
            db.log("resync_recovered", detail=f"recovered {added}")
            notify("recovered", f"رجعت {added} رسالة كانت فايتة من القناة", level="success")
        return added

    async def _resync_loop(self):
        while True:
            await asyncio.sleep(TICK)
            since = monotonic() - self._last_resync
            day = datetime.now(db.TZ).date().isoformat()
            gap = db.missing_day_counts(day)
            if since >= RESYNC_EVERY or (gap and since >= GAP_RESYNC_EVERY):
                try:
                    await self.resync()
                except Exception as e:
                    log.exception("resync failed")
                    db.log("error", detail=f"resync: {e}")
                    notify("error", "مش قادر أقرا القناة", str(e)[:200], level="error", important=True,
                           throttle="resync")
                    continue
                # only warn about counters a resync couldn't recover; most gaps fill themselves within a minute
                still = set(gap) & set(db.missing_day_counts(day))
                if new_gap := db.new_today("gap_notified", day, still):
                    notify("gap", "في رسايل ناقصة من القناة", "عداد إجمالي اليوم: " + "، ".join(sorted(new_gap, key=int))
                           + ". جرّب \"هات الرسايل الناقصة\"", level="warning", important=True)

    async def fetch(self, ids: list[int]) -> dict:
        """Read-only: the channel's current copy of these messages, {id: message}. Empty if not connected."""
        if not ids or self.client is None or self.channel is None or not self.client.is_connected():
            return {}
        msgs = await self.client.get_messages(self.channel, ids=ids)
        return {m.id: m for m in msgs if m}

    async def start(self) -> int:
        async with self._lock:
            if self.running:
                return 0
            self.error = None
            try:
                await self._ensure_connected()
                self.client.add_event_handler(self._on_message, events.NewMessage(chats=self.channel))
                self.client.add_event_handler(self._on_edit, events.MessageEdited(chats=self.channel))
                await self.client.catch_up()  # replay queued updates, now that the handler can see them
                self.running = True
                db.set_setting("listener_enabled", 1)
                added = await self.catch_up()
                self._last_resync = monotonic()
                self._resync_task = asyncio.create_task(self._resync_loop())
                db.log("listener_start", detail=f"caught up {added}")
                notify("listener", f"بدأ يسمع القناة – جاب {added} رسالة فاتت", level="success")
                return added
            except Exception as e:
                self.error = str(e)
                raise

    async def stop(self):
        async with self._lock:
            if self._resync_task:
                self._resync_task.cancel()
                self._resync_task = None
            if self.client:
                self.client.remove_event_handler(self._on_message)
                self.client.remove_event_handler(self._on_edit)
            self.running = False
            db.set_setting("listener_enabled", 0)
            db.log("listener_stop")
            notify("listener", "وقف سماع القناة")

    async def shutdown(self):
        if self._resync_task:
            self._resync_task.cancel()
        if self.client:
            await self.client.disconnect()
