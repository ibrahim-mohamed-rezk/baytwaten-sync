"""Check the dashboard connection WITHOUT changing anything.

    python check_dashboard.py 163
Logs in, searches for the plot number and prints what it understood from the page.
"""
import asyncio
import sys

from dotenv import load_dotenv

from app.console import utf8_console

utf8_console()
load_dotenv()
from app import dashboard  # noqa: E402


async def main(plot: str):
    if not dashboard.is_configured():
        sys.exit("❌ حط DASHBOARD_EMAIL و DASHBOARD_PASSWORD (أو DASHBOARD_COOKIE) في .env")
    rows = await dashboard.search(plot)
    print(f"✅ الدخول تمام. CSRF: {'موجود' if dashboard._csrf else '❌ مش موجود'}")
    if not rows:
        print("⚠️ مالقيتش ولا قطعة في الصفحة. ابعت ملف debug_page.html")
        resp = await dashboard._request("GET", f"{dashboard._prefix()}/land_plots",
                                        params={"q": plot, "per_page": 500})
        open("debug_page.html", "w", encoding="utf-8").write(resp.text)
    for r in rows:
        print(f"  #{r['id']}  قطعة {r['plot']}  | {r['city']} | {r['region']} | "
              f"{r['neighborhood'] or '-'} | {r['area']} م² | الحالة: {r['status']}")
    await dashboard._get_client().aclose()

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "163"))
