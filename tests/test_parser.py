"""Run: python -m tests.test_parser"""
import re
from pathlib import Path

from app.dashboard import match
from app.parser import normalize, parse_reservation, plot_key

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
