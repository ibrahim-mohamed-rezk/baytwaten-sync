// Shared by every page: small helpers, toasts, and the header (nav links + live status + listener controls).
// Load it at the end of <body>, before the page's own script.

const $ = id => document.getElementById(id);
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// "2026-10-28T14:19:08" -> "2:19 م" (12-hour clock everywhere)
const t12 = iso => { if (!iso) return ""; const [h, m] = iso.slice(11, 16).split(":").map(Number);
  return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "ص" : "م"}`; };
const STATUS = { pending:"منتظر", done:"اتحدّث", failed:"محتاج مراجعة", rejected:"مرفوض", duplicate:"مكرر" };

async function api(path, method = "GET") {
  const r = await fetch(path, { method });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}

// log=false for messages that are only about the page itself (they'd clutter the notifications list)
function toast(msg, log = true) {
  let t = $("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2500);
  if (!log) return;
  const level = msg.startsWith("⚠️") ? "warning" : msg.startsWith("✅") ? "success" : "info";
  fetch("/api/notifications/panel", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: msg, level }) }).catch(() => {});
}

const Shell = (() => {
  // to add a page: one entry here, one FileResponse route in app/main.py
  const LINKS = [
    { href: "/", icon: "📋", label: "الحجوزات" },
    { href: "/compare", icon: "📊", label: "مقارنة مع مدينتي",
      badge: s => s.compare?.wrong, hint: "حجوزات النهارده في مدينتي من غير رسالة تليجرام" },
    { href: "/notifications", icon: "🔔", label: "الإشعارات",
      badge: s => s.notifications_unread, hint: "إشعارات مهمة مش مقروءة" },
  ];
  const here = location.pathname.replace(/\/+$/, "") || "/";
  const subs = [];
  let state = null;

  document.body.insertAdjacentHTML("afterbegin", `
  <header class="shell">
    <a class="brand" href="/"><span class="logo">🏗</span><span><b>بيت الوطن</b><small>مزامنة الحجوزات مع مدينتي</small></span></a>
    <nav>${LINKS.map(l => `<a href="${l.href}" class="${l.href === here ? "active" : ""}">${l.icon} <span>${l.label}</span>
      <b class="badge" data-badge="${l.href}" title="${esc(l.hint || "")}" hidden></b></a>`).join("")}</nav>
    <div class="state">
      <span class="st" id="stListener"><i></i><span>…</span></span>
      <span class="st" id="stDash"><i></i><span>…</span></span>
      <span class="st" id="stQueue" title="بيشوف القطع المنتظرة لو كانت اتحجزت في الداشبورد خلاص" hidden>🔄 <span></span></span>
      <label class="st switch" id="stAuto" title="تحديث الداشبورد تلقائي للقطع المطابقة 100%">
        <input type="checkbox" id="autoMode"><span>تحديث تلقائي</span></label>
      <button id="listenerBtn">…</button>
    </div>
  </header>`);

  const chip = (id, cls, text, title = "") => {
    const el = $(id);
    el.className = "st " + cls;
    el.lastElementChild.textContent = text;
    el.title = title;
  };

  function paint(s) {
    const L = s.listener;
    if (L.error) chip("stListener", "bad", "القناة: فيه مشكلة", L.error);
    else if (L.running) chip("stListener", "ok live", "بيسمع القناة",
      L.last_message_at ? "آخر رسالة " + t12(L.last_message_at) : "");
    else chip("stListener", "warn", "القناة واقفة", "مش بيقرا رسايل جديدة لحد ما تدوس تشغيل");
    if (s.dashboard_configured) chip("stDash", "ok", "مدينتي جاهزة", s.dashboard_url);
    else chip("stDash", "warn", "مدينتي مش متظبطة", "اكتب بيانات الدخول في .env");
    $("stQueue").hidden = !s.queue;
    $("stQueue").lastElementChild.textContent = `بيراجع… (${s.queue})`;
    $("autoMode").checked = s.auto_mode;
    $("autoMode").disabled = !s.dashboard_configured;
    $("stAuto").className = "st switch" + (s.auto_mode ? " on" : "") + (s.dashboard_configured ? "" : " disabled");
    const b = $("listenerBtn");
    if (!b._busy) { b.textContent = L.running ? "⏸ إيقاف" : "▶ تشغيل"; b.className = L.running ? "stop" : "primary"; }
    for (const l of LINKS) {
      const el = document.querySelector(`[data-badge="${l.href}"]`), n = l.badge?.(s) || 0;
      el.hidden = !n; el.textContent = n;
    }
  }

  async function refresh() {
    try { state = await api("/api/state"); }
    catch (e) { chip("stListener", "bad", "السيرفر مش شغال", e.message); throw e; }
    paint(state);
    for (const fn of subs) await fn(state);
    return state;
  }

  $("listenerBtn").onclick = async () => {
    const b = $("listenerBtn");
    b.disabled = b._busy = true;
    try {
      if (state.listener.running) { await api("/api/listener/stop", "POST"); toast("⏸ اتوقف"); }
      else { b.textContent = "بيشغّل…"; const r = await api("/api/listener/start", "POST"); toast(`▶ اشتغل – جاب ${r.caught_up} رسالة فاتت`); }
    } catch (err) { toast("⚠️ " + err.message); }
    b.disabled = b._busy = false;
    await refresh().catch(() => {});
  };
  $("autoMode").onchange = async e => {
    try { await api(`/api/auto-mode/${e.target.checked ? 1 : 0}`, "POST"); }
    catch (err) { toast("⚠️ " + err.message); }
    await refresh().catch(() => {});
  };

  setInterval(() => refresh().catch(() => {}), 5000);
  return {
    refresh,
    get state() { return state; },
    // fn(state) runs after every refresh (the 5s poll included)
    onState(fn) { subs.push(fn); },
  };
})();
