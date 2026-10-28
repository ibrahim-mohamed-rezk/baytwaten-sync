"""Parse 'تم حجز قطعة' messages from the Bayt Al-Watan channel."""
import re

HEADER = "تم حجز قطعة"

_AR_NORMALIZE = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي",
    "ـ": None,  # tatweel
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
})
_DIACRITICS = re.compile(r"[ً-ْ]")


def normalize(s: str | None) -> str:
    """Normalize Arabic text so 'القاهرة الجديدة' == 'القاهره الجديده'."""
    if not s:
        return ""
    s = _DIACRITICS.sub("", s).translate(_AR_NORMALIZE)
    s = re.sub(r"[()\-–_|,،.]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _search(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None


def _field(label: str, text: str) -> str | None:
    return _search(r"•\s*" + label + r"\s*:\s*(.+)", text)


def _number(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s.translate(_AR_NORMALIZE))
    return float(m.group()) if m else None


def parse_reservation(text: str) -> dict | None:
    """Return the reservation fields, or None if this isn't a reservation message."""
    if not text or HEADER not in text:
        return None
    plot = _field("رقم القطعة", text)
    city = _search(r"🏙\s*(.+)", text)
    if not plot or not city:
        return None

    reserved = _search(r"محجوز\s*(\d+)\s*من", text)
    total = _search(r"محجوز\s*\d+\s*من\s*(\d+)", text)
    day_count = _search(r"إجمالي اليوم\s*:\s*(\d+)", text)
    return {
        "day_count": int(day_count) if day_count else None,
        "city": city,
        "sector": _search(r"🏘\s*(.+)", text),
        "block": _field("المربع", text),
        "plot": plot.translate(_AR_NORMALIZE),
        "area": _number(_field("المساحة", text)),
        "features": _field("التميز", text),
        "down_payment": _field("المقدم", text),
        "project_reserved": int(reserved) if reserved else None,
        "project_total": int(total) if total else None,
        "updated_at": _search(r"تاريخ التحديث\s*:\s*(.+)", text),
    }


def plot_key(r: dict) -> str:
    """Identity of a plot: city + sector + block + plot number."""
    return "|".join(normalize(r.get(k)) for k in ("city", "sector", "block", "plot"))
