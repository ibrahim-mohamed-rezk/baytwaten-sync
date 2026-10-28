"""READ-ONLY: find today's channel message with a given daily counter and explain why the parser skips it.

    python -m scripts.find_message 67

Works on a temp copy of telegram.session, so it's safe while the panel is running. Writes nothing.
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, time, timezone

from dotenv import load_dotenv

from app.console import utf8_console

utf8_console()
load_dotenv()
from telethon import TelegramClient  # noqa: E402

from app.listener import SESSION, resolve_channel  # noqa: E402
from app.db import TZ  # noqa: E402
from app.parser import HEADER, _AR_NORMALIZE, _field, _search, parse_reservation  # noqa: E402

COUNTER = re.compile(r"اجمالي\s*اليوم\s*:?\s*(\d+)")


def counter(text: str) -> int | None:
    m = COUNTER.search((text or "").translate(_AR_NORMALIZE))
    return int(m.group(1)) if m else None


def why_rejected(text: str | None) -> str:
    """Replays parse_reservation()'s checks in order and names the first one that fails."""
    if not text:
        return "msg.message is empty (media-only?) -> save_message() returns before parsing"
    if HEADER not in text:
        return f"HEADER {HEADER!r} not in text -> `if not text or HEADER not in text: return None`"
    if not _field("رقم القطعة", text):
        return "_field('رقم القطعة') matched nothing (expects '• رقم القطعة : <x>') -> `if not plot ...: return None`"
    if not _search(r"🏙\s*(.+)", text):
        return "no '🏙 <city>' line -> `if ... not city: return None`"
    return "parse_reservation() ACCEPTS it"


async def main(n: int):
    tmp = tempfile.mkdtemp()
    shutil.copy(SESSION + ".session", os.path.join(tmp, "copy.session"))
    client = TelegramClient(os.path.join(tmp, "copy"), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    try:
        channel = await resolve_channel(client, os.environ["TG_CHANNEL"])
        today = datetime.combine(datetime.now(TZ).date(), time.min, TZ).astimezone(timezone.utc)
        msgs = [m async for m in client.iter_messages(channel, offset_date=today, reverse=True)]
        print(f"today: {len(msgs)} channel messages, ids {msgs[0].id}..{msgs[-1].id}" if msgs else "no messages today")

        print(f"\n--- counters {n - 2}..{n + 2} (to see where the sequence breaks) ---")
        for m in msgs:
            c = counter(m.message)
            if c is not None and abs(c - n) <= 2:
                kind = "reservation" if parse_reservation(m.message) else "NOT PARSED"
                print(f"id={m.id} counter={c} {kind} | {(m.message or '').splitlines()[0][:70]!r}")

        hits = [m for m in msgs if counter(m.message) == n]
        print(f"\n--- messages with counter {n}: {len(hits)} ---")
        for m in hits:
            print(f"\nid={m.id} date={m.date.astimezone(TZ)} edit_date={m.edit_date and m.edit_date.astimezone(TZ)}")
            print("parse_reservation ->", parse_reservation(m.message))
            print("why:", why_rejected(m.message))
            print("plots in text ('رقم القطعة' occurrences):", (m.message or "").count("رقم القطعة"))
            print("raw:", repr(m.message))

        # nothing carries the counter? show everything between its neighbours, which is where it should be
        if not any(parse_reservation(m.message) for m in hits):
            before = max((m.id for m in msgs if (counter(m.message) or 0) == n - 1), default=None)
            after = min((m.id for m in msgs if (counter(m.message) or 0) == n + 1), default=None)
            between = [m for m in msgs if before and after and before < m.id < after]
            print(f"\n--- all messages between counter {n - 1} (id {before}) and {n + 1} (id {after}): {len(between)} ---")
            for m in between:
                print(f"\nid={m.id} date={m.date.astimezone(TZ)} edit_date={m.edit_date and m.edit_date.astimezone(TZ)}")
                print("why:", why_rejected(m.message))
                print("raw:", repr(m.message))
    finally:
        await client.disconnect()
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 67))
