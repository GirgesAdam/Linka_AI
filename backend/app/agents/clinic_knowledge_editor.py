from __future__ import annotations

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.clinic_knowledge_edit_transport import (
    KnowledgeEditTransportError,
    _FlatKnowledgeEditDecision,
    _normalize_flat_decision,
    _transport_catalog,
)
from app.agents.llm_runtime import LLMProviderError, is_cross_model_failover_eligible
from app.agents.model_provider import build_onboarding_fallback_model, build_onboarding_model
from app.agents.structured_output import StructuredOutputError, invoke_typed_structured_output
from app.schemas.agent_knowledge import KnowledgeEditDecision

logger = logging.getLogger(__name__)


def propose_knowledge_edit(*, message: str, catalog: dict) -> KnowledgeEditDecision:
    transport_catalog, ref_to_id = _transport_catalog(catalog)
    messages = [
        SystemMessage(
            content=(
                "You are Linka's clinic knowledge editing assistant. Understand ONE administrator request "
                "about the clinic data shown in the supplied canonical catalog and propose a small, safe, "
                "structured change set. Return only the required structured output. Never claim that a write "
                "already happened. The backend will validate every target and requires explicit admin confirmation.\n\n"
                "The catalog uses short request-local refs such as service:0, branch:1, doctor:2. Select ONLY refs "
                "that exist in the supplied catalog. Do not output UUIDs. Match the administrator's natural wording "
                "semantically to the entity names in the catalog. If more than one entity is a plausible match, set "
                "needs_clarification=true, return no operations, and ask one short Egyptian-Arabic question.\n\n"
                "Output FLAT operations. One primitive field change = one operation. If the administrator changes two "
                "fields on the same service in one sentence, return two update_service operations with the SAME target_ref. "
                "Example: duration=45 and price=2000 means two operations; Python will group them into one confirmed edit.\n\n"
                "Allowed operation kinds:\n"
                "- update_service: target_ref=service ref; field may be name, category, description, duration_minutes, "
                "price_egp, requires_medical_review, is_active.\n"
                "- update_branch: target_ref=branch ref; field may be name, city, address_line1, phone, timezone, is_active.\n"
                "- update_doctor: target_ref=doctor ref; field may be first_name, last_name, phone, email, specialization, "
                "booking_enabled, is_active.\n"
                "- set_branch_hours: target_ref=branch ref; emit one operation per OPEN interval with weekday/start_time/end_time. "
                "The emitted intervals together are the FULL desired weekly schedule; omitted weekdays are closed.\n"
                "- set_doctor_hours: target_ref=doctor ref, branch_ref=branch ref; emit one operation per OPEN interval.\n"
                "- set_doctor_services: target_ref=doctor ref; related_refs is the COMPLETE desired set of service refs.\n"
                "- set_doctor_branches: target_ref=doctor ref; related_refs is the COMPLETE desired set of branch refs; "
                "primary_branch_ref may be one of them.\n"
                "- update_booking_settings: no target_ref; one primitive field change per operation.\n\n"
                "For schedule requests, understand natural Arabic expressions such as السبت للخميس, الجمعة إجازة, "
                "10 الصبح لـ10 بالليل. Monday=0 ... Sunday=6. If the user gives only a partial schedule and does not "
                "clearly intend to replace the full week, ask for clarification instead of guessing closed days.\n"
                "For every primitive field change, set value_type to exactly text, number, or boolean and put the extracted value "
                "as plain text in value. Examples: duration 45 => value_type=number, value=45; active => value_type=boolean, "
                "value=true. Leave field/value_type/value empty or none only for non-field operations. For prices, put pounds in price_egp "
                "(not minor units). Keep assistant_message concise and in Egyptian Arabic. Patients and appointments are "
                "operational records: do not propose changes to them in this assistant; explain that their dedicated pages should be used."
            )
        ),
        HumanMessage(
            content=(
                "ADMIN_REQUEST:\n" + message + "\n\nCANONICAL_CLINIC_CATALOG_JSON:\n" +
                json.dumps(transport_catalog, ensure_ascii=False, separators=(",", ":"))
            )
        ),
    ]

    def invoke(model):
        flat = invoke_typed_structured_output(
            model=model,
            schema=_FlatKnowledgeEditDecision,
            messages=messages,
        )
        return _normalize_flat_decision(flat, ref_to_id=ref_to_id)

    try:
        return invoke(build_onboarding_model())
    except (LLMProviderError, StructuredOutputError, KnowledgeEditTransportError) as exc:
        fallback = build_onboarding_fallback_model()
        if fallback is None:
            raise
        if isinstance(exc, LLMProviderError) and not is_cross_model_failover_eligible(exc):
            raise
        logger.warning("Clinic knowledge edit primary model failed; trying fallback (%s).", type(exc).__name__)
        return invoke(fallback)
