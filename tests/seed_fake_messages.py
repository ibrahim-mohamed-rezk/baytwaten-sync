"""Push tests/sample_messages.txt through the listener as if they came from Telegram (no account needed).

Run: python -m tests.seed_fake_messages
"""
import asyncio
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()
from app.listener import Listener  # noqa: E402
from tests.test_parser import messages  # noqa: E402


class FakeMessage:
    def __init__(self, msg_id, text, minutes_ago):
        self.id, self.message = msg_id, text
        self.date = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


async def main():
    listener = Listener()
    msgs = messages()
    for i, text in enumerate(msgs):
        plot_id = await listener.save_message(FakeMessage(100 + i, text, (len(msgs) - i) * 5))
        print(i, "->", plot_id)

asyncio.run(main())
