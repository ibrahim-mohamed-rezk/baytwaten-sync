"""Talks to our Laravel dashboard as a logged-in user — no backend changes needed.

- Search:  GET  /{locale}/dashboard/land_plots?q=<plot>&per_page=500   (full HTML page)
- Reserve: POST /{locale}/dashboard/land_plots/{id}/update-status      {"status": "reserved"}  (AJAX, JSON)
"""
import logging
import os
import re
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

from .parser import normalize


class NotConfigured(Exception):
    pass


class AuthError(Exception):
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def is_configured() -> bool:
    return bool(_env("DASHBOARD_BASE_URL") and (
        (_env("DASHBOARD_EMAIL") and _env("DASHBOARD_PASSWORD")) or _env("DASHBOARD_COOKIE")))


def _prefix() -> str:
    return f"/{_env('DASHBOARD_LOCALE', 'ar')}/dashboard"


_client: httpx.AsyncClient | None = None
_csrf: str | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if not is_configured():
        raise NotConfigured("بيانات الدخول على الداشبورد مش متظبطة في .env")
    if _client is None:
        headers = {"User-Agent": "Mozilla/5.0 (baytwaten-sync)"}
        if cookie := _env("DASHBOARD_COOKIE"):
            headers["Cookie"] = cookie
        _client = httpx.AsyncClient(base_url=_env("DASHBOARD_BASE_URL"), headers=headers,
                                    follow_redirects=True, timeout=30)
    return _client


def _is_login_page(resp: httpx.Response) -> bool:
    return resp.status_code in (401, 419) or "login" in resp.url.path


def _extract_csrf(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    inp = soup.find("input", attrs={"name": "_token"})
    return inp.get("value") if inp else None


async def login() -> None:
    """Log in with email/password (Laravel form login). Skipped when using a pasted cookie."""
    global _csrf
    email, password = _env("DASHBOARD_EMAIL"), _env("DASHBOARD_PASSWORD")
    if not (email and password):
        raise AuthError("الكوكي انتهت. حدّث DASHBOARD_COOKIE في .env، أو حط الإيميل والباسورد")
    c = _get_client()
    login_path = _env("DASHBOARD_LOGIN_PATH", "/dashboard/login")
    page = await c.get(login_path)
    token = _extract_csrf(page.text)
    if not token:
        raise AuthError(f"مش لاقي فورم تسجيل الدخول على {login_path}")
    resp = await c.post(login_path, data={
        "_token": token, _env("DASHBOARD_LOGIN_FIELD", "email"): email,
        "password": password, "remember": "on",
    })
    if _is_login_page(resp):
        raise AuthError("تسجيل الدخول على الداشبورد فشل. اتأكد من الإيميل والباسورد")
    _csrf = _extract_csrf(resp.text)


async def _request(method: str, url: str, **kw) -> httpx.Response:
    """Send a request; if the session expired, log in again and retry once."""
    c = _get_client()
    resp = await c.request(method, url, **kw)
    if _is_login_page(resp):
        await login()
        if "headers" in kw:
            kw["headers"] = {**kw["headers"], **_ajax_headers()}
        resp = await c.request(method, url, **kw)
        if _is_login_page(resp):
            raise AuthError("الداشبورد رافض الدخول حتى بعد تسجيل الدخول")
    resp.raise_for_status()
    return resp


# ---------- search (HTML page) ----------

COLUMNS = {  # normalized header text -> field
    "#": "row_id", "رقم القطعه": "plot", "المرحله": "phase", "الموقع": "city",
    "المنطقه": "region", "المجاوره": "neighborhood", "المساحه": "area", "الحاله": "status",
}
PLOT_URL = re.compile(r"land_plots/(\d+)(?:/|\b)")


def _number(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


def _status_of(cell) -> str:
    """The status cell is a dropdown — read the current value, not every option's text."""
    opt = cell.select_one("option[selected]")
    if opt:
        return opt.get("value") or opt.get_text(" ", strip=True)
    for el in cell.select("[data-status]"):
        return el["data-status"]
    btn = cell.select_one(".dropdown-toggle, button")
    return (btn or cell).get_text(" ", strip=True)


def classify_status(raw: str) -> str:
    s = normalize(raw)
    if "reserved" in s or "محجوز" in s:
        return "reserved"
    if "available" in s or "متاح" in s:
        return "available"
    return raw.strip() or "unknown"


def parse_plots_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for table in soup.find_all("table"):
        heads = [normalize(th.get_text(" ", strip=True)) for th in table.select("thead th")]
        idx = {}
        for i, h in enumerate(heads):  # "المساحة (م²)" -> "المساحه م²" still counts as المساحه
            key = next((k for k in COLUMNS if h == k or h.startswith(k + " ")), None)
            if key and COLUMNS[key] not in idx:
                idx[COLUMNS[key]] = i
        if "plot" not in idx:
            continue
        for tr in table.select("tbody tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < len(heads):
                continue
            cell = lambda f: tds[idx[f]] if f in idx else None  # noqa: E731
            text = lambda f: cell(f).get_text(" ", strip=True) if cell(f) else ""  # noqa: E731
            m = PLOT_URL.search(str(tr))
            plot_id = m.group(1) if m else (re.sub(r"\D", "", text("row_id")) or None)
            if not plot_id:
                continue
            out.append({
                "id": plot_id, "plot": text("plot"), "phase": text("phase"), "city": text("city"),
                "region": text("region"), "neighborhood": text("neighborhood"),
                "area": _number(text("area")),
                "status": classify_status(_status_of(cell("status"))) if cell("status") else "unknown",
            })
    return out


PER_PAGE = 500  # the dashboard accepts up to 1000; anything above silently falls back to 50
MAX_PAGES = 20


async def _get_page(q: str, page: int, per_page: int) -> list[dict]:
    global _csrf
    resp = await _request("GET", f"{_prefix()}/land_plots", params={"q": q, "per_page": per_page, "page": page})
    _csrf = _extract_csrf(resp.text) or _csrf
    return parse_plots_page(resp.text)


async def search(plot_number: str) -> list[dict]:
    """All rows whose plot number contains `plot_number`, across every page.

    q is a substring search over all cities, so a short number has many hits (q=10 -> 655 rows). One page
    of 500 used to drop السادات "C10" and report it as not found.
    """
    out: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        rows = await _get_page(plot_number, page, PER_PAGE)
        new = {r["id"]: r for r in rows if r["id"] not in out}
        out.update(new)
        if len(rows) < PER_PAGE or not new:
            break
    else:
        logging.getLogger("dashboard").warning("search %r: stopped after %d pages", plot_number, MAX_PAGES)
    return list(out.values())


# ---------- reserve (AJAX) ----------

def _ajax_headers() -> dict:
    h = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
         "Referer": f"{_env('DASHBOARD_BASE_URL')}{_prefix()}/land_plots"}
    if _csrf:
        h["X-CSRF-TOKEN"] = _csrf
    if _client and (xsrf := _client.cookies.get("XSRF-TOKEN")):
        h["X-XSRF-TOKEN"] = unquote(xsrf)
    return h


async def reserve(dashboard_id) -> dict:
    if not _csrf:
        await _get_page("0", 1, 1)  # any page load gives us the CSRF token
    resp = await _request(_env("DASHBOARD_UPDATE_METHOD", "POST"),
                          f"{_prefix()}/land_plots/{dashboard_id}/update-status",
                          json={"status": "reserved"}, headers=_ajax_headers())
    body = resp.json()
    if not body.get("success") or (body.get("plot") or {}).get("status") != "reserved":
        raise RuntimeError("الداشبورد مأكدتش الحجز: " + str(body.get("message") or body)[:200])
    return body["plot"]


# ---------- matching ----------

# "163" | "163 5" (from "163-5") | "163a" | "ca23" (letters first, العاشر من رمضان).
# A digit suffix needs a separator, so "1635" stays one number. Letters on both sides don't parse.
PLOT_NO = re.compile(r"^(?:([^\W\d_]+)\s*)?(\d+)(?:\s*([^\W\d_]+)|\s+(\d+))?$")


def _split_plot(s: str | None) -> tuple[str, str | None] | None:
    m = PLOT_NO.match(normalize(s))  # no space-stripping: "163 5" must not become "1635"
    if not m or (m.group(1) and (m.group(3) or m.group(4))):
        return None
    return str(int(m.group(2))), m.group(1) or m.group(3) or m.group(4)


# Neighborhood code at the end of a block/region/المجاورة: Latin letters or digits after منطقة/المجاورة.
# Spaces are dropped, so "منطقة ( T I)" -> "ti", "الحى 24 -منطقة TJ" -> "tj", "المجاورة ٥" -> "5".
CODE = re.compile(r"(?:منطقه|مجاوره)\s+([a-z0-9]+(?:\s+[a-z0-9]+)*)$")


def _code(s: str | None) -> str | None:
    m = CODE.search(normalize(s))
    return m.group(1).replace(" ", "") if m else None


def same_plot_number(dash_plot: str, tg_plot: str, tg_block: str | None = None) -> bool:
    """Some cities keep the neighborhood in the plot number ("163-5", "163A", "TI38") while Telegram sends
    the bare number and puts the neighborhood in المربع ("المجاورة 5", "منطقة ( T I)"). The dashboard
    suffix must then equal the block's last token or its neighborhood code; a different neighborhood never matches."""
    a, b = _split_plot(dash_plot), _split_plot(tg_plot)
    if not (a and b):
        return normalize(dash_plot) == normalize(tg_plot)
    if a[0] != b[0]:
        return False
    if a[1] == b[1]:
        return True
    block = normalize(tg_block).split()
    return b[1] is None and a[1] in {block[-1] if block else None, _code(tg_block)} - {None}


def _neighborhood(c: dict) -> str | None:
    """A dashboard row's neighborhood code: from the plot number, else المجاورة, else the region."""
    split = _split_plot(c["plot"])
    return (split and split[1]) or _code(c.get("neighborhood")) or _code(c.get("region"))

def area_matches(c: dict, plot: dict) -> bool:
    """Telegram rounds the area (594 vs 594.20), so < 1 m². Unknown on either side doesn't count against it."""
    return not (c.get("area") and plot.get("area")) or abs(c["area"] - plot["area"]) < 1


def match(plot: dict, candidates: list[dict], region_alias: str | None = None) -> tuple[list[dict], bool]:
    """Narrow search results down to the plot from the Telegram message.

    Returns (candidates, confident). Confident = city, region, plot number (with neighborhood) and area all
    agree (Telegram 🏙 = الموقع, 🏘 = المنطقة). Area also breaks ties (Telegram rounds: 594 vs 594.20).
    The region may also equal the block's part before " - ": 🏘 "المحور المركزى" with block
    "المحور المركزى الثانى - منطقة D" is the dashboard's "المحور المركزى الثانى". Exact match only, no prefix guessing.
    A row whose neighborhood code contradicts Telegram's (TJ vs "منطقة ( T I)") is never a candidate,
    not even as a suggestion.
    """
    block = plot.get("block") or ""
    tg_split = _split_plot(plot["plot"])
    tg_code = (tg_split and tg_split[1]) or _code(block)
    same = [c for c in candidates
            if same_plot_number(c["plot"], plot["plot"], block)
            and normalize(c["city"]) == normalize(plot["city"])
            and not (tg_code and _neighborhood(c) not in (None, tg_code))]
    wanted = {normalize(plot["sector"]), normalize(block), normalize(re.split(r"\s+[-–]\s+", block)[0])}
    if region_alias:
        wanted.add(normalize(region_alias))
    by_region = [c for c in same if normalize(c["region"]) in wanted - {""}]
    pool = by_region or same
    if len(pool) > 1 and plot.get("area"):
        pool = [c for c in pool if c.get("area") and abs(c["area"] - plot["area"]) < 1] or pool
    return pool, bool(by_region) and len(pool) == 1 and area_matches(pool[0], plot)
