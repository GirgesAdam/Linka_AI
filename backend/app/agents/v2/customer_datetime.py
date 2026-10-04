from __future__ import annotations

from datetime import date, datetime

ARABIC_WEEKDAYS = (
    "الاثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)
ENGLISH_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
ARABIC_MONTHS = {
    1: "يناير",
    2: "فبراير",
    3: "مارس",
    4: "أبريل",
    5: "مايو",
    6: "يونيو",
    7: "يوليو",
    8: "أغسطس",
    9: "سبتمبر",
    10: "أكتوبر",
    11: "نوفمبر",
    12: "ديسمبر",
}


def parse_customer_date(value: object) -> date | None:
    if isinstance(value, dict):
        value = value.get("start_date") or value.get("date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None


def format_customer_date(
    value: object,
    *,
    arabic: bool,
    reference_date: date | None = None,
) -> str:
    parsed = parse_customer_date(value)
    if parsed is None:
        return str(value)
    reference = reference_date or date.today()
    include_year = parsed.year != reference.year
    if arabic:
        rendered = f"{ARABIC_WEEKDAYS[parsed.weekday()]} {parsed.day} {ARABIC_MONTHS[parsed.month]}"
        return f"{rendered} {parsed.year}" if include_year else rendered
    rendered = f"{ENGLISH_WEEKDAYS[parsed.weekday()]}, {parsed.strftime('%B')} {parsed.day}"
    return f"{rendered}, {parsed.year}" if include_year else rendered
