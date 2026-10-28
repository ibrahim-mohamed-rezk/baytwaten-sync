"""One-time Telegram login. Run once: python login.py"""
import asyncio
from dotenv import load_dotenv

from app.console import utf8_console

utf8_console()
load_dotenv()
from app.listener import make_client, resolve_channel  # noqa: E402
import os  # noqa: E402


async def main():
    client = make_client()
    await client.start()  # asks for phone number + code (and 2FA password if set)
    me = await client.get_me()
    print(f"✅ تم تسجيل الدخول: {me.first_name} ({me.phone})")
    channel = await resolve_channel(client, os.environ["TG_CHANNEL"])
    print(f"✅ القناة: {channel.title}")
    await client.disconnect()

asyncio.run(main())
