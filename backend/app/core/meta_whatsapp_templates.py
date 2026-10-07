from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TemplateCategory = Literal["UTILITY", "MARKETING"]


@dataclass(frozen=True)
class StandardWhatsAppTemplate:
    rule_key: str
    label_ar: str
    name: str
    language: str
    category: TemplateCategory
    body_text: str
    example_body_parameters: tuple[str, ...]
    quick_reply_buttons: tuple[str, ...] = ()


STANDARD_WHATSAPP_TEMPLATES: tuple[StandardWhatsAppTemplate, ...] = (
    StandardWhatsAppTemplate(
        rule_key="appointment_reminder_6h",
        label_ar="تأكيد الحضور قبل الموعد",
        name="tia_appointment_confirmation_01",
        language="ar_EG",
        category="UTILITY",
        body_text=(
            "أهلًا {{1}} 👋 ميعاد {{2}} بتاعك يوم {{3}} الساعة {{4}}. "
            "ياريت تأكد حضورك من الزر تحت، ولو محتاج تغيّر الميعاد اختار تغيير الميعاد 💛"
        ),
        example_body_parameters=("مريم", "ليزر", "15/10/2026", "18:00"),
        quick_reply_buttons=("تأكيد الحضور", "تغيير الميعاد"),
    ),
    StandardWhatsAppTemplate(
        rule_key="appointment_reminder_legacy",
        label_ar="تذكير قبل الموعد",
        name="tia_reminder_01",
        language="ar_EG",
        category="UTILITY",
        body_text="أهلًا {{1}} 👋 بفكرك إن عندك جلسة {{2}} الساعة {{3}}. مستنيينك 💛",
        example_body_parameters=("مريم", "ليزر", "17:00"),
    ),
    StandardWhatsAppTemplate(
        rule_key="appointment_reminder_legacy",
        label_ar="تذكير قبل الموعد",
        name="tia_reminder_02",
        language="ar_EG",
        category="UTILITY",
        body_text="أهلًا {{1}} 👋 بس تذكير صغير إن عندك {{2}} الساعة {{3}}. نشوفك على خير 💛",
        example_body_parameters=("مريم", "ليزر", "17:00"),
    ),
    StandardWhatsAppTemplate(
        rule_key="appointment_reminder_legacy",
        label_ar="تذكير قبل الموعد",
        name="tia_reminder_03",
        language="ar_EG",
        category="UTILITY",
        body_text="هاي {{1}} 🌿 موعد {{2}} بتاعك الساعة {{3}}. مستنيينك، ولو في أي حاجة ابعتلنا هنا.",
        example_body_parameters=("مريم", "ليزر", "17:00"),
    ),
    StandardWhatsAppTemplate(
        rule_key="post_visit_followup",
        label_ar="متابعة بعد الزيارة",
        name="tia_post_visit_01",
        language="ar_EG",
        category="UTILITY",
        body_text="إزيك {{1}}؟ حبيت أطمن عليكي بعد {{2}} اللي كانت يوم {{3}}. كل حاجة تمام؟",
        example_body_parameters=("مريم", "ليزر", "12/09/2026"),
    ),
    StandardWhatsAppTemplate(
        rule_key="post_visit_followup",
        label_ar="متابعة بعد الزيارة",
        name="tia_post_visit_02",
        language="ar_EG",
        category="UTILITY",
        body_text="أهلًا {{1}} 💛 حابين نطمن إن كل حاجة تمام بعد {{2}} اللي كانت يوم {{3}}. لو في أي سؤال إحنا موجودين.",
        example_body_parameters=("مريم", "ليزر", "12/09/2026"),
    ),
    StandardWhatsAppTemplate(
        rule_key="post_visit_followup",
        label_ar="متابعة بعد الزيارة",
        name="tia_post_visit_03",
        language="ar_EG",
        category="UTILITY",
        body_text="هاي {{1}} 👋 بنطمن بس بعد {{2}} يوم {{3}}. كل حاجة ماشية كويس؟ 💛",
        example_body_parameters=("مريم", "ليزر", "12/09/2026"),
    ),
    StandardWhatsAppTemplate(
        rule_key="cancellation_recovery",
        label_ar="متابعة بعد الإلغاء",
        name="tia_cancellation_recovery_ar",
        language="ar_EG",
        category="UTILITY",
        body_text=(
            "أهلًا {{1}}، حبيت أساعدك بعد إلغاء موعد {{2}} يوم {{3}} الساعة {{4}}. "
            "لو حابة نرتب ميعاد جديد ابعتيلي هنا وأنا أساعدك."
        ),
        example_body_parameters=("مريم", "ليزر", "12/09/2026", "17:00"),
    ),
    StandardWhatsAppTemplate(
        rule_key="cancellation_recovery",
        label_ar="متابعة بعد الإلغاء",
        name="tia_cancellation_recovery_02",
        language="ar_EG",
        category="UTILITY",
        body_text=(
            "أهلًا {{1}} 💛 بخصوص موعد {{2}} يوم {{3}} الساعة {{4}}، "
            "لو تحب نرتب وقت أنسب ابعتلنا هنا ونساعدك."
        ),
        example_body_parameters=("مريم", "ليزر", "12/09/2026", "17:00"),
    ),
    StandardWhatsAppTemplate(
        rule_key="cancellation_recovery",
        label_ar="متابعة بعد الإلغاء",
        name="tia_cancellation_recovery_03",
        language="ar_EG",
        category="UTILITY",
        body_text=(
            "هاي {{1}} 👋 شوفنا إن موعد {{2}} يوم {{3}} الساعة {{4}} اتلغى. "
            "لو تحب نلاقي ميعاد جديد يناسبك، إحنا موجودين."
        ),
        example_body_parameters=("مريم", "ليزر", "12/09/2026", "17:00"),
    ),
    StandardWhatsAppTemplate(
        rule_key="lead_not_booked_followup",
        label_ar="متابعة العميل اللي ماحجزش",
        name="tia_ai_followup_ar",
        language="ar_EG",
        category="MARKETING",
        body_text=(
            "أهلًا {{1}}، بنتابع معاكي بخصوص {{2}}. آخر متابعة كانت يوم {{3}} الساعة {{4}} من {{5}}. "
            "لو حابة تكملي الحجز ابعتيلي هنا."
        ),
        example_body_parameters=("مريم", "استفسار عن الليزر", "12/09/2026", "17:00", "Linka Clinic"),
    ),
    StandardWhatsAppTemplate(
        rule_key="lead_not_booked_followup",
        label_ar="متابعة العميل اللي ماحجزش",
        name="tia_ai_followup_02",
        language="ar_EG",
        category="MARKETING",
        body_text=(
            "أهلًا {{1}} 👋 رجعنا نطمن بخصوص {{2}}. آخر تواصل كان يوم {{3}} الساعة {{4}} مع {{5}}. "
            "لو في أي سؤال أو تحب تكمل الحجز ابعتلنا هنا."
        ),
        example_body_parameters=("مريم", "استفسار عن الليزر", "12/09/2026", "17:00", "Linka Clinic"),
    ),
    StandardWhatsAppTemplate(
        rule_key="lead_not_booked_followup",
        label_ar="متابعة العميل اللي ماحجزش",
        name="tia_ai_followup_03",
        language="ar_EG",
        category="MARKETING",
        body_text=(
            "هاي {{1}} 💛 رجعنا نطمن بخصوص {{2}} علشان نتأكد إن كل التفاصيل واضحة ومناسبة ليك. "
            "آخر تواصل كان يوم {{3}} الساعة {{4}} مع {{5}}. "
            "لو حابب/حابة تكمل الحجز أو عندك أي سؤال، ابعتلنا هنا وإحنا نساعدك."
        ),
        example_body_parameters=("مريم", "استفسار عن الليزر", "12/09/2026", "17:00", "Linka Clinic"),
    ),
)


STANDARD_TEMPLATES_BY_RULE_KEY: dict[str, tuple[StandardWhatsAppTemplate, ...]] = {
    rule_key: tuple(
        template for template in STANDARD_WHATSAPP_TEMPLATES if template.rule_key == rule_key
    )
    for rule_key in dict.fromkeys(template.rule_key for template in STANDARD_WHATSAPP_TEMPLATES)
}

# Backwards-compatible canonical template: the first variant is the long-lived default name.
STANDARD_TEMPLATE_BY_RULE_KEY = {
    rule_key: templates[0]
    for rule_key, templates in STANDARD_TEMPLATES_BY_RULE_KEY.items()
}


def approved_standard_templates(
    rule_key: str,
    statuses: dict[str, str],
) -> tuple[StandardWhatsAppTemplate, ...]:
    return tuple(
        template
        for template in STANDARD_TEMPLATES_BY_RULE_KEY.get(rule_key, ())
        if str(statuses.get(template.name) or "").lower() == "approved"
    )


def approved_template_refs_for_rule(
    rule_key: str,
    statuses: dict[str, str],
) -> list[dict[str, str]]:
    return [
        {"name": template.name, "language_code": template.language}
        for template in approved_standard_templates(rule_key, statuses)
    ]


def template_create_payload(template: StandardWhatsAppTemplate) -> dict[str, object]:
    components: list[dict[str, object]] = [
        {
            "type": "BODY",
            "text": template.body_text,
            "example": {"body_text": [list(template.example_body_parameters)]},
        }
    ]
    if template.quick_reply_buttons:
        components.append(
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": title}
                    for title in template.quick_reply_buttons
                ],
            }
        )
    return {
        "name": template.name,
        "language": template.language,
        "category": template.category,
        "components": components,
    }
