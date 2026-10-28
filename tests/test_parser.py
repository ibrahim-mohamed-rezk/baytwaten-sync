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
    assert len({plot_key(p) for p in parsed if p}) == 5


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
