"""Run: python -m tests.test_parser"""
import re
from pathlib import Path

from app.console import utf8_console
from app.dashboard import match
from app.parser import normalize, parse_reservation, plot_key

utf8_console()

PREFIX = re.compile(r"^\[[^\]]+\] [^:]+: ", re.M)


def messages():
    text = (Path(__file__).parent / "sample_messages.txt").read_text(encoding="utf-8")
    return [m.strip() for m in PREFIX.split(text) if m.strip()]


def test_parse():
    msgs = messages()
    parsed = [parse_reservation(m) for m in msgs]
    assert parsed[2] is None, "hourly summary must be skipped"
    r = parsed[1]
    assert r["city"] == "العبور الجديدة" and r["sector"] == "الحى الرابع و العشرون"
    assert r["block"] == "الحى الرابع و العشرون - منطقة ( T F)"
    assert r["plot"] == "38" and r["area"] == 629 and r["day_count"] == 20
    assert r["project_reserved"] == 482 and r["project_total"] == 495
    assert parsed[3]["features"] == "🔷 ناصية | 🌳 حديقة"
    assert len({plot_key(p) for p in parsed if p}) == 6


def test_counter_67():
    """The message behind today's gap (msg 16244): raw Telegram text, no URL after the last line."""
    r = parse_reservation(messages()[-1])
    assert (r["day_count"], r["city"], r["sector"]) == (67, "العاشر من رمضان", "منطقة المال و الاعمال(عمارات)")
    assert (r["block"], r["plot"], r["area"]) == ("منطقة المال و الاعمال - منطقة CA", "31", 500)
    assert (r["down_payment"], r["project_reserved"], r["project_total"]) == ("31K $", 102, 473)


def test_normalize():
    assert normalize("القاهرة الجديدة") == normalize("القاهره الجديده")
    assert normalize("الامتداد الجنوبى بالقرب من ديستركت 5") == normalize("الامتداد الجنوبي بالقرب من ديستركت ٥")


def test_match():
    tg = {"city": "القاهرة الجديدة", "sector": "الامتداد الجنوبى بالقرب من ديستركت 5", "plot": "163", "area": 594}
    rows = [
        {"id": 9414, "city": "القاهره الجديده", "region": "الامتداد الجنوبى بالقرب من ديستركت 5", "plot": "163", "area": 594.2, "status": "متاح"},
        {"id": 1, "city": "السادات", "region": "المحور المركزى", "plot": "163", "area": 500, "status": "متاح"},
        {"id": 2, "city": "القاهره الجديده", "region": "منطقة أخرى", "plot": "163", "area": 700, "status": "متاح"},
    ]
    pool, confident = match(tg, rows)
    assert [c["id"] for c in pool] == [9414] and confident
    # region named differently in the dashboard -> single candidate, but not confident
    rows[0]["region"] = "ديستركت 5 جنوب"
    pool, confident = match(tg, rows)
    assert [c["id"] for c in pool] == [9414] and not confident
    # ...until the alias is learned
    assert match(tg, rows, region_alias="ديستركت 5 جنوب")[1]


def test_match_real_rows():
    # real rows from the dashboard for q=163 (q is a substring search across all cities)
    rows = [{"id": i, "plot": p, "city": c, "region": r, "neighborhood": n, "area": a, "status": "available"}
            for i, p, c, r, n, a in [
                (23000, "163", "العبور الجديدة", "الحى 24 -منطقة TJ", "—", 469.1),
                (21690, "163A", "اسيوط الجديدة", "التوسعات الشمالية المجاورة الرابعة الحى الخامس", "المجاورة A", 456.0),
                (17854, "163-5", "السادات", "الحى المتميز", "المجاورة ٥", 630.0),
                (17432, "163-2", "السادات", "الحى المتميز", "المجاورة ٢", 630.0),
                (9414, "163", "القاهره الجديدة", "الامتداد الجنوبى بالقرب من ديستركت 5", "—", 594.2),
                (1251, "163", "القاهره الجديدة", "الحى الرابع منطقة بيت وطن Q", "—", 437.5),
            ]]
    cases = [
        ("العبور الجديدة", "الحى 24 -منطقة TJ", "المنطقة TJ", 469, 23000),
        ("أسيوط الجديدة", "التوسعات الشمالية المجاورة الرابعة الحى الخامس", "منطقة A", 456, 21690),
        ("السادات", "الحى المتميز", "المجاورة 5", 630, 17854),
        ("السادات", "الحى المتميز", "المجاورة 2", 630, 17432),
        ("القاهرة الجديدة", "الامتداد الجنوبى بالقرب من ديستركت 5", "الامتداد الجنوبى بالقرب من ديستركت 5", 594, 9414),
    ]
    for city, sector, block, area, want in cases:
        tg = {"city": city, "sector": sector, "block": block, "plot": "163", "area": area}
        pool, confident = match(tg, rows)
        assert [c["id"] for c in pool] == [want] and confident, (block, pool)
    # a neighborhood that doesn't exist never falls back to another one
    tg = {"city": "السادات", "sector": "الحى المتميز", "block": "المجاورة 3", "plot": "163", "area": 630}
    assert match(tg, rows)[0] == []


def test_same_plot_number():
    from app.dashboard import same_plot_number
    assert same_plot_number("163-5", "163", "المجاورة 5")
    assert not same_plot_number("163-5", "163", "المجاورة 3")
    assert same_plot_number("163A", "163", "منطقة A")
    assert same_plot_number("0163", "163")
    assert same_plot_number("163-5", "163-5")
    assert not same_plot_number("1635", "163", "المجاورة 5")
    assert not same_plot_number("163-5", "163-2", "المجاورة 5")
    # العاشر من رمضان puts the neighborhood first: "CA23"
    assert same_plot_number("CA23", "23", "منطقة المال و الاعمال - منطقة CA")
    assert not same_plot_number("CB23", "23", "منطقة المال و الاعمال - منطقة CA")
    assert not same_plot_number("CA123", "23", "منطقة CA")
    assert same_plot_number("ca-23", "CA23")


def test_match_prefix_rows():
    """Real rows for q=23 in العاشر من رمضان (2026-10-28). Telegram: plot 23, block "... منطقة CA", 500 m²."""
    region = "منطقة المال و الاعمال (عمارات)"
    rows = [{"id": i, "plot": p, "city": "العاشر من رمضان", "region": region, "neighborhood": n, "area": a,
             "status": "available"} for i, p, n, a in [
        (21313, "123CC", "المجاورة CC", 438.7), (21358, "23CD", "المجاورة CD", 562.6),
        (21405, "23CE", "المجاورة CE", 500.0), (21213, "23CC", "المجاورة CC", 556.2),
        (3488, "CB23", "المجاورة CB", 483.4), (3420, "CA23", "المجاورة CA", 500.0)]]
    tg = {"city": "العاشر من رمضان", "sector": "منطقة المال و الاعمال(عمارات)",
          "block": "منطقة المال و الاعمال - منطقة CA", "plot": "23", "area": 500}
    pool, confident = match(tg, rows)
    assert [c["id"] for c in pool] == [3420] and confident, pool
    tg["block"] = "منطقة المال و الاعمال - منطقة CF"
    assert match(tg, rows)[0] == []


def test_match_spaced_neighborhood_code():
    """Real العبور rows for q=38 (2026-10-28). Telegram writes the code with a space: "منطقة ( T I)".
    It used to fall back to the bare "38" in district TJ and suggest that one."""
    rows = [{"id": i, "plot": p, "city": "العبور الجديده", "region": r, "neighborhood": n, "area": a,
             "status": "reserved"} for i, p, r, n, a in [
        (22875, "38", "الحى 24 -منطقة TJ", "—", 481.6),
        (3310, "TG38", "الحى الرابع و العشرون", "المجاورة TG", 645.8),
        (3172, "TH38", "الحى الرابع و العشرون", "المجاورة TH", 500.0),
        (3047, "TI38", "الحى الرابع و العشرون", "المجاورة TI", 645.8),
        (2931, "TF38", "الحى الرابع و العشرون", "المجاورة TF", 628.6)]]
    tg = {"city": "العبور الجديدة", "sector": "الحى الرابع و العشرون", "plot": "38", "area": 629}
    # TF38 is 628.6 m² ~ 629 -> confident. TI38 is 645.8 m² -> right plot, but unsure until confirmed.
    for code, want, sure in (("( T I)", 3047, False), ("( T F)", 2931, True)):
        tg["block"] = f"الحى الرابع و العشرون - منطقة {code}"
        pool, confident = match(tg, rows)
        assert [c["id"] for c in pool] == [want] and confident == sure, (code, pool, confident)
    tg["block"], tg["area"] = "الحى الرابع و العشرون - منطقة ( T I)", 645
    assert match(tg, rows)[1], "within 1 m² -> confident"
    tg["area"] = 629
    # a code the dashboard doesn't have -> nothing, and never the TJ plot as a suggestion
    tg["block"] = "الحى الرابع و العشرون - منطقة ( T K)"
    assert match(tg, rows)[0] == []
    # Telegram's own TJ block still finds the bare "38" in district TJ
    tg.update(sector="الحى 24 -منطقة TJ", block="المنطقة TJ", area=481)
    assert [c["id"] for c in match(tg, rows)[0]] == [22875]


def test_match_region_from_block():
    """Real السادات rows (2026-10-28): 🏘 says "المحور المركزى" but the dashboard region is the block's
    part before " - "."""
    region = "المحور المركزى الثانى"
    rows = [{"id": i, "plot": p, "city": "السادات", "region": r, "neighborhood": "", "area": a, "status": s}
            for i, p, r, a, s in [
                (4834, "E29", region, 500.0, "available"), (4675, "D29", region, 500.0, "reserved"),
                (4588, "C29", region, 500.0, "available"), (4490, "B29", region, 510.0, "reserved"),
                (21057, "29-4", "الحى الرابع - حى الزهور", 600.3, "available")]]
    tg = {"city": "السادات", "sector": "المحور المركزى", "block": "المحور المركزى الثانى - منطقة D",
          "plot": "29", "area": 500}
    pool, confident = match(tg, rows)
    assert [c["id"] for c in pool] == [4675] and confident, pool
    # a block whose first part is NOT the region stays unsure (suggestion only)
    tg["block"] = "المحور المركزى - منطقة D"
    pool, confident = match(tg, rows)
    assert [c["id"] for c in pool] == [4675] and not confident, pool


def test_dashboard_page():
    from app.dashboard import parse_plots_page
    html = """<table><thead><tr><th></th><th>#</th><th>رقم<br>القطعة</th><th>الموقع</th><th>المنطقة</th>
      <th>المجاورة</th><th>المساحة (م²)</th><th>الحالة</th><th>الإجراءات</th></tr></thead><tbody>
      <tr><td><input type=checkbox></td><td>9414</td><td>163</td><td>القاهره الجديده</td><td>ديستركت 5</td><td>—</td>
      <td>m² 594.20</td><td><button class="dropdown-toggle">محجوز</button><ul><li>متاح</li><li>محجوز</li></ul></td>
      <td><a href="/ar/dashboard/land_plots/9414/edit">تعديل</a></td></tr></tbody></table>"""
    [r] = parse_plots_page(html)
    assert (r["id"], r["plot"], r["area"], r["status"]) == ("9414", "163", 594.2, "reserved")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("✓", name)
