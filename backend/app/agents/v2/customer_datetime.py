from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _customer_reference_date(
    *,
    reference_date: date | None,
    reference_datetime: datetime | None,
    timezone_name: str | None,
) -> date:
    if reference_date is not None:
        return reference_date
    if reference_datetime is None:
        return date.today()
    if timezone_name:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone = None
        if timezone is not None:
            if reference_datetime.tzinfo is None:
                reference_datetime = reference_datetime.replace(tzinfo=timezone)
            else:
                reference_datetime = reference_datetime.astimezone(timezone)
    return reference_datetime.date()


def format_customer_date(
    value: object,
    *,
    arabic: bool,
    reference_date: date | None = None,
    reference_datetime: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    parsed = parse_customer_date(value)
    if parsed is None:
        return str(value)
    use_relative_label = reference_date is not None or reference_datetime is not None
    reference = _customer_reference_date(
        reference_date=reference_date,
        reference_datetime=reference_datetime,
        timezone_name=timezone_name,
    )
    include_year = parsed.year != reference.year
    if arabic:
        if use_relative_label and parsed == reference:
            return "\u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647"
        if use_relative_label and parsed == reference + timedelta(days=1):
            return "\u0628\u0643\u0631\u0629"
        rendered = f"{ARABIC_WEEKDAYS[parsed.weekday()]} {parsed.day} {ARABIC_MONTHS[parsed.month]}"
        return f"{rendered} {parsed.year}" if include_year else rendered
    rendered = f"{ENGLISH_WEEKDAYS[parsed.weekday()]}, {parsed.strftime('%B')} {parsed.day}"
    return f"{rendered}, {parsed.year}" if include_year else rendered
