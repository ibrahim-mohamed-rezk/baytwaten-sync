# baytwaten-sync – Telegram → dashboard reservation sync

## What this is
A standalone local tool. It reads plot-reservation messages ("🏗 تم حجز قطعة") from an external Telegram
**channel** (بيت الوطن | موقع م. ربيع). It shows them in its own panel at http://localhost:8800, and on approval
it marks the same plot as reserved in **our** Laravel dashboard (madinty).

The user speaks Egyptian Arabic, so reply in Egyptian Arabic. Code, comments and commits stay in English.
UI text is Egyptian Arabic, RTL.

## Hard constraints (decided with the user, don't revisit)
- **Never modify the dashboard backend.** We act as a logged-in user over HTTP, the same requests the browser sends.
- **The panel stays separate from the dashboard.** No changes to dashboard pages.
- **The Telegram channel isn't ours.** It's private, we're only members, and it's a channel, not a group.
  So no bot: we read it with Telethon on a member account (`login.py` makes `telegram.session`).
- **The user can start/stop listening any time.** On start it catches up on everything posted while stopped
  (`last_msg_id` in settings), so nothing is lost.
- **Safety over automation.** The dashboard is only changed on a certain match. Anything doubtful goes to "محتاج مراجعة".
  Auto mode is opt-in, via a checkbox in the panel.
- Runs locally on the user's Windows machine (`start.bat`). A VPS is possible later.

## Layout
- `app/parser.py`: parses a message into city/sector/block/plot/area/features/down_payment/day_count/project counts.
  `normalize()` folds Arabic variants (ة/ه, ى/ي, أ/ا, digits, punctuation). `plot_key` = city|sector|block|plot.
  Hourly "🔥 الأكثر حجزًا" summaries are ignored.
- `app/listener.py`: Telethon client. Resolves the invite link via `CheckChatInviteRequest`, then runs a NewMessage handler and `catch_up()`.
- `app/db.py`: SQLite (`data.sqlite3`). Tables: `plots`, `settings`, `region_aliases`, `log`.
  - Plot statuses: pending | done | rejected | failed | duplicate.
  - `missing_day_counts()` uses the channel's sequential "إجمالي اليوم" counter to detect missed messages.
  - Small ALTER-based migrations live in the module body.
- `app/dashboard.py`: the dashboard client. See "Dashboard facts" below.
- `app/main.py`: FastAPI app. `approve()` is the core decision logic. `/api/*` endpoints. Auto mode is handled in `on_new_plot`.
- `app/static/index.html`: the panel. Vanilla JS, polls `/api/state` and `/api/plots` every 5s.
- `check_dashboard.py`: read-only connection check (login + search, prints parsed rows, dumps `debug_page.html` if nothing parsed).
- `tests/`:
  - `test_parser.py`: plain asserts, run with `python -m tests.test_parser`.
  - `sample_messages.txt`: real channel messages.
  - `fake_dashboard.py`: mock of the Laravel dashboard.
  - `seed_fake_messages.py`: pushes the samples through the listener with no Telegram account.

## Dashboard facts (from the user's DevTools captures)
- Base: `https://madinty.test-domain-devunity.com`. This is a test domain; production may differ (`DASHBOARD_BASE_URL`).
- Login: `/dashboard/login`. A form POST to the same URL with fields `_token` (hidden CSRF), `email`, `password`
  and `remember`. Verified from the live page.
- Search is a **server-rendered page**, not JSON:
  `GET /ar/dashboard/land_plots?q=<plot>&phase_id=&zone_id=<id>&district_id=&distinction=&status=&reserved_from=&reserved_to=&per_page=500`.
  - We send only `q` and `per_page=500`, then filter locally.
  - `q` is a substring search across all cities: `q=163` also returns `163A` and `163-5`.
  - Table columns: # | رقم القطعة | المرحلة | الموقع (city) | المنطقة (region) | المجاورة | المساحة (م²) | … | الحالة (dropdown button) | تاريخ الحجز | الإجراءات (تعديل).
  - `parse_plots_page()` maps columns by header text. It takes the id from a `land_plots/<id>` link in the row, falling back to the # column.
- Reserve: **POST** `/ar/dashboard/land_plots/{id}/update-status`, JSON `{"status":"reserved"}`.
  - Needs the session cookie plus an `X-CSRF-TOKEN` header (taken from `<meta name="csrf-token">` on any page).
  - Response: `{success:true, message:"تم التحديث بنجاح", plot:{id, plot_number, area, status:"reserved", land_zone_id, ...}}`.
- Matching: Telegram 🏙 = الموقع, 🏘 = المنطقة. Areas are rounded in Telegram (594 vs 594.20), so the tolerance is < 1 m².
  - Some cities keep the neighborhood in the plot number (السادات `163-5`, أسيوط الجديدة `163A`), while Telegram
    sends the bare `163` and puts the neighborhood in المربع (`المجاورة 5`). `same_plot_number()` splits a number
    into (base, suffix) and matches when the bases agree and either the suffixes are equal or Telegram has none and
    the dashboard suffix equals the block's last token. A different neighborhood never matches.
  - If the city and number match but the region name differs, the plot becomes `failed` with `suggest_id`.
  - The "اربط وحدّث" button stores a `region_aliases` row and reserves the plot.

## Status (2026-10-28)
Done and tested locally: the parser on real messages, the panel UI, dedupe and gap detection, and the whole approve flow
against `tests/fake_dashboard.py` (auto re-login on expired session, CSRF, confident match, region alias, not found,
already reserved).

`check_dashboard.py 163` verified against the real dashboard: login and table parsing work. Its rows exposed the
neighborhood-in-plot-number case, now covered by `test_match_real_rows`.

**Not yet verified against the real systems:**
1. A real reserve POST from the panel.
2. The real Telethon connection to the private channel (`login.py`, then ▶ in the panel).

Next ideas, only if the user asks:
- Use `zone_id` in the search instead of local filtering.
- Handle cancellation messages, if the channel ever posts them.
- Run on a VPS.
- Desktop or sound notification on new plots.

## Run / test
```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m tests.test_parser
.venv\Scripts\python -m uvicorn tests.fake_dashboard:app --port 9900          # mock dashboard
.venv\Scripts\python -m tests.seed_fake_messages                             # fake channel messages
.venv\Scripts\python run.py                                                  # panel on :8800
```
Never commit `.env`, `telegram.session` or `data.sqlite3`. They hold credentials and live data.
