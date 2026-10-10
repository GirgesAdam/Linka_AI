from __future__ import annotations

from app.services.agent_v2.outcome import TurnOutcome


def _comparison_price(facts: dict[str, object]) -> str | None:
    wrapper = facts.get("service_catalog")
    if not isinstance(wrapper, dict):
        return None
    service = wrapper.get("service")
    if not isinstance(service, dict):
        return None
    price = str(service.get("price") or "").strip()
    if price:
        return price
    selected = service.get("selected_laser_device")
    if isinstance(selected, dict):
        selected_price = str(selected.get("price") or "").strip()
        if selected_price:
            return selected_price
    return None


def _package_rows(facts: dict[str, object]) -> list[dict[str, object]]:
    wrapper = facts.get("customer_packages")
    if not isinstance(wrapper, dict):
        return []
    rows = wrapper.get("packages")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def deterministic_package_comparison_reply(
    outcomes: list[TurnOutcome],
) -> tuple[str, str] | None:
    outcome = next((item for item in outcomes if item.response_goal == "package_comparison"), None)
    if outcome is None:
        return None
    facts = outcome.facts if isinstance(outcome.facts, dict) else {}
    price = _comparison_price(facts)
    rows = _package_rows(facts)
    usable = [
        row
        for row in rows
        if str(row.get("effective_status") or "").lower() == "active"
        and int(row.get("sessions_remaining") or 0) > 0
    ]

    price_sentence = (
        f"سعر الجلسة الفردية {price}."
        if price
        else "سعر الجلسة الفردية مش ظاهر عندي بشكل مؤكد في البيانات الحالية."
    )
    if len(usable) == 1:
        row = usable[0]
        name = str(row.get("name") or "الباكدج").strip()
        remaining = int(row.get("sessions_remaining") or 0)
        device = str(row.get("laser_device_name") or "").strip()
        device_text = f" على {device}" if device else ""
        return (
            f"{price_sentence} وعندك {name}{device_text} فعالة للخدمة ولسه فيها {remaining} جلسة. "
            "ينفع تستخدمي جلسة منها بدل دفع جلسة منفصلة؛ الاختيار ليكي.",
            "deterministic:package-comparison",
        )
    if len(usable) > 1:
        options = "، ".join(
            f"{str(row.get('name') or 'باكدج').strip()} ({int(row.get('sessions_remaining') or 0)} جلسة متبقية)"
            for row in usable
        )
        return (
            f"{price_sentence} وعندك أكتر من باكدج فعالة تنطبق على الخدمة: {options}. "
            "مش هختار باكدج منهم من غير ما تحددي أنهي واحدة تقصدي.",
            "deterministic:package-comparison",
        )
    if not rows:
        return (
            f"{price_sentence} ومش ظاهر عندك حاليًا باكدج تنطبق على الخدمة دي، "
            "فمفيش جلسة باكدج متاحة أخصم منها دلوقتي.",
            "deterministic:package-comparison",
        )

    statuses = {str(row.get("effective_status") or "").lower() for row in rows}
    if statuses == {"expired"}:
        state = "الباكدج المسجلة للخدمة منتهية الصلاحية"
    elif statuses <= {"exhausted", "active"} and all(int(row.get("sessions_remaining") or 0) <= 0 for row in rows):
        state = "الباكدج المسجلة للخدمة مفيهاش جلسات متبقية"
    else:
        state = "الباكدج المسجلة للخدمة مش قابلة للاستخدام حاليًا"
    return (
        f"{price_sentence} و{state}، فمش هاعتبرها رصيد متاح للجلسة دي.",
        "deterministic:package-comparison",
    )
