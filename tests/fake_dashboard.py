"""Mock of our Laravel dashboard for local testing (form login, HTML search page, AJAX update-status).

Run:  python -m uvicorn tests.fake_dashboard:app --port 9900
Then in .env: DASHBOARD_BASE_URL=http://127.0.0.1:9900  DASHBOARD_LOGIN_PATH=/login  DASHBOARD_EMAIL=any  DASHBOARD_PASSWORD=pw
"""
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
app = FastAPI()
PLOTS = {
  "9414": dict(plot="163", phase="المرحلة الحادية عشر", city="القاهره الجديده", region="الامتداد الجنوبى بالقرب من ديستركت 5", nb="", area=594.20, status="available"),
  "8905": dict(plot="271", phase="المرحلة الأولى", city="العبور الجديدة", region="الحى 24 -منطقة TJ", nb="", area=500, status="available"),
  "7001": dict(plot="38", phase="1", city="العبور الجديدة", region="الحى الرابع والعشرون منطقة TF", nb="", area=629, status="available"),
  "7002": dict(plot="38", phase="1", city="السادات", region="المحور المركزى", nb="", area=500, status="reserved"),
  # two identical candidates -> ambiguous; one reserved plot only reachable through a region alias
  "8906": dict(plot="500", phase="1", city="العبور الجديدة", region="الحى الخامس", nb="", area=400, status="reserved"),
  "8907": dict(plot="500", phase="1", city="العبور الجديدة", region="الحى الخامس", nb="", area=400, status="reserved"),
  "8908": dict(plot="612", phase="1", city="العبور الجديدة", region="الحى 24 -منطقة TJ", nb="", area=500, status="reserved"),
}
SESS = "s3cret"; CSRF = "tok123"
POSTS: list[str] = []  # ids that received update-status, so tests can prove a check stayed read-only
def authed(req): return req.cookies.get("laravel_session") == SESS
@app.get("/login", response_class=HTMLResponse)
def login_page(): return f'<form method=post><input name="_token" value="{CSRF}"><input name=email><input name=password></form>'
@app.post("/login")
async def do_login(req: Request):
    f = await req.form(); password = f.get("password")
    if f.get("_token") != CSRF or password != "pw": return RedirectResponse("/login", 302)
    r = RedirectResponse("/ar/dashboard", 302); r.set_cookie("laravel_session", SESS); r.set_cookie("XSRF-TOKEN", "x%3Dy"); return r
@app.get("/ar/dashboard", response_class=HTMLResponse)
def home(req: Request): return "home" if authed(req) else RedirectResponse("/login", 302)
STATUS_AR = {"available": "متاح", "reserved": "محجوز"}
@app.get("/ar/dashboard/land_plots", response_class=HTMLResponse)
def plots(req: Request, q: str = ""):
    if not authed(req): return RedirectResponse("/login", 302)
    rows = "".join(f'''<tr><td><input type=checkbox value="{i}"></td><td>{i}</td><td>{p["plot"]}</td><td><span>{p["phase"]}</span></td>
      <td>{p["city"]}</td><td>{p["region"]}</td><td>{p["nb"] or "—"}</td><td>m² {p["area"]:.2f}</td>
      <td><div class="dropdown"><button class="dropdown-toggle">{STATUS_AR[p["status"]]}</button><ul><li>متاح</li><li>محجوز</li><li>مباع</li></ul></div></td>
      <td>—</td><td><a href="/ar/dashboard/land_plots/{i}/edit">تعديل</a></td></tr>''' for i, p in PLOTS.items() if q in p["plot"])
    return f'''<html><head><meta name="csrf-token" content="{CSRF}"></head><body><table><thead><tr><th></th><th>#</th><th>رقم<br>القطعة</th><th>المرحلة</th>
      <th>الموقع</th><th>المنطقة</th><th>المجاورة</th><th>المساحة (م²)</th><th>الحالة</th><th>تاريخ الحجز</th><th>الإجراءات</th></tr></thead><tbody>{rows}</tbody></table></body></html>'''
@app.post("/ar/dashboard/land_plots/{pid}/update-status")
async def upd(pid: str, req: Request):
    if not authed(req): return JSONResponse({"message": "Unauthenticated."}, 401)
    if req.headers.get("x-csrf-token") != CSRF: return JSONResponse({"message": "CSRF token mismatch."}, 419)
    POSTS.append(pid)
    PLOTS[pid]["status"] = (await req.json())["status"]
    return {"success": True, "message": "تم التحديث بنجاح", "plot": {"id": int(pid), "status": PLOTS[pid]["status"]}}
