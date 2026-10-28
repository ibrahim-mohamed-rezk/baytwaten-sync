"""Reads the Telegram channel with a member account (the channel isn't ours, so no bot)."""
import asyncio
import logging
import os
from datetime import datetime, time, timezone

from telethon import TelegramClient, events
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready

from . import db
from .parser import parse_reservation, plot_key

log = logging.getLogger("listener")
SESSION = os.path.join(os.path.dirname(__file__), "..", "telegram")


def make_client() -> TelegramClient:
    if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
        raise RuntimeError("TG_API_ID و TG_API_HASH مش متظبطين في ملف .env")
    return TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])


async def resolve_channel(client: TelegramClient, ref: str):
    """Accepts a private invite link (t.me/+hash), @username or numeric id."""
    if "/+" in ref or "joinchat/" in ref:
        invite_hash = ref.rstrip("/").split("/")[-1].lstrip("+")
        invite = await client(CheckChatInviteRequest(invite_hash))
        if not isinstance(invite, ChatInviteAlready):
            raise RuntimeError("الحساب ده مش عضو في القناة. ادخلها من التليجرام الأول.")
        return invite.chat
    return await client.get_entity(int(ref) if ref.lstrip("-").isdigit() else ref)


class Listener:
    def __init__(self, on_new_plot=None):
        self.client: TelegramClient | None = None
        self.channel = None
        self.running = False
        self.error: str | None = None
        self.last_message_at: str | None = None
        self.on_new_plot = on_new_plot  # async callback(plot_id), used for auto mode
        self._lock = asyncio.Lock()

    async def _ensure_connected(self):
        if self.client is None:
            self.client = make_client()
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("لازم تسجل دخول التليجرام الأول: شغّل python login.py")
        if self.channel is None:
            self.channel = await resolve_channel(self.client, os.environ["TG_CHANNEL"])

    async def save_message(self, msg) -> int | None:
        if not msg.message:
            return None
        r = parse_reservation(msg.message)
        if not r:
            return None
        posted = msg.date.astimezone(db.TZ).replace(tzinfo=None).isoformat(timespec="seconds")
        plot_id = db.insert_plot(msg.id, posted, r, plot_key(r), msg.message)
        if msg.id > int(db.get_setting("last_msg_id", 0)):
            db.set_setting("last_msg_id", msg.id)
        if plot_id:
            self.last_message_at = posted
            if self.on_new_plot:
                await self.on_new_plot(plot_id)
        return plot_id

    async def _on_message(self, event):
        try:
            await self.save_message(event.message)
        except Exception as e:  # never let one bad message kill the listener
            log.exception("failed to save message %s", event.message.id)
            db.log("error", detail=f"msg {event.message.id}: {e}")

    async def catch_up(self) -> int:
        """Fetch what was posted while we were stopped (or today's messages on first run)."""
        await self._ensure_connected()
        last_id = int(db.get_setting("last_msg_id", 0))
        today = datetime.combine(datetime.now(db.TZ).date(), time.min, db.TZ).astimezone(timezone.utc)
        added = 0
        # oldest first so the daily counter and "last_msg_id" advance in order
        async for msg in self.client.iter_messages(self.channel, min_id=last_id, reverse=True,
                                                   offset_date=None if last_id else today):
            if await self.save_message(msg):
                added += 1
        return added

    async def start(self) -> int:
        async with self._lock:
            if self.running:
                return 0
            self.error = None
            try:
                await self._ensure_connected()
                self.client.add_event_handler(self._on_message, events.NewMessage(chats=self.channel))
                self.running = True
                db.set_setting("listener_enabled", 1)
                added = await self.catch_up()
                db.log("listener_start", detail=f"caught up {added}")
                return added
            except Exception as e:
                self.error = str(e)
                raise

    async def stop(self):
        async with self._lock:
            if self.client:
                self.client.remove_event_handler(self._on_message)
            self.running = False
            db.set_setting("listener_enabled", 0)
            db.log("listener_stop")

    async def shutdown(self):
        if self.client:
            await self.client.disconnect()
