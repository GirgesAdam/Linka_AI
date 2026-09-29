from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    ServiceInformationItem,
    ServiceTruth,
    is_pure_supported_service_contract,
)


class ServiceInfoComposerValidationError(RuntimeError):
    pass


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    return " ".join(message.content.strip().split())[:limit]


def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message)
        if text:
            return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _render_catalog(truth: ServiceTruth, *, arabic: bool) -> str:
    names = [item.name for item in truth.services]
    if not names:
        return (
            "مفيش خدمات مسجلة في كتالوج العيادة الحالي."
            if arabic
            else "There are no services in the current clinic catalog."
        )
    joined = "، ".join(names) if arabic else ", ".join(names)
    return (
        f"الخدمات المسجلة في كتالوج العيادة: {joined}."
        if arabic
        else f"Services in the clinic catalog: {joined}."
    )


def _render_description(
    item: ServiceInformationItem,
    *,
    arabic: bool,
) -> str:
    if item.clinic_explanation:
        return (
            f"المعلومات التوضيحية المحفوظة من العيادة: {item.clinic_explanation}"
            if arabic
            else f"Clinic-saved explanatory information: {item.clinic_explanation}"
        )
    return (
        "مفيش معلومات توضيحية محفوظة من العيادة أقدر أعرضها للخدمة دي."
        if arabic
        else "There is no clinic-saved explanatory information to show for this service."
    )


def _render_duration(
    item: ServiceInformationItem,
    *,
    arabic: bool,
) -> str:
    if item.customer_duration_text:
        return (
            f"مدة الحجز المسجلة للخدمة: {item.customer_duration_text}."
            if arabic
            else f"Recorded booking duration: {item.customer_duration_text}."
        )
    if item.booking_duration_minutes is not None:
        return (
            f"مدة الحجز المسجلة للخدمة: {item.booking_duration_minutes} دقيقة."
            if arabic
            else f"Recorded booking duration: {item.booking_duration_minutes} minutes."
        )
    return (
        "مدة الحجز مش مسجلة في البيانات الحالية."
        if arabic
        else "A booking duration is not recorded in the current data."
    )


def _render_devices(
    item: ServiceInformationItem,
    *,
    arabic: bool,
) -> str:
    if item.devices:
        joined = "، ".join(item.devices) if arabic else ", ".join(item.devices)
        return (
            f"الأجهزة المهيأة للخدمة في بيانات العيادة: {joined}."
            if arabic
            else f"Devices configured for the service in clinic data: {joined}."
        )

    return (
        "مفيش جهاز ليزر مهيأ للخدمة في بيانات العيادة الحالية."
        if arabic
        else "No laser device is configured for the service in the current clinic data."
    )


def _render_detail(truth: ServiceTruth, *, arabic: bool) -> str:
    if len(truth.services) != 1:
        raise ServiceInfoComposerValidationError(
            "Service-detail truth must contain exactly one service."
        )
    item = truth.services[0]
    lines = [
        f"الخدمة المسجلة: {item.name}."
        if arabic
        else f"Recorded service: {item.name}."
    ]
    requested = set(truth.requested_details)
    if "description" in requested:
        lines.append(_render_description(item, arabic=arabic))
    if "duration" in requested:
        lines.append(_render_duration(item, arabic=arabic))
    if "devices" in requested:
        lines.append(_render_devices(item, arabic=arabic))
    return "\n".join(lines)


def deterministic_service_info_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_service_contract(contract):
        raise ServiceInfoComposerValidationError(
            "Service-info composer requires a pure supported service contract."
        )


    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.service_truth
        if truth is None:
            raise ServiceInfoComposerValidationError(
                "Service information truth is missing."
            )
        if truth.kind == "service_catalog":
            chunks.append(_render_catalog(truth, arabic=arabic))
        elif truth.kind == "service_detail":
            chunks.append(_render_detail(truth, arabic=arabic))
        else:
            raise ServiceInfoComposerValidationError(
                f"Unsupported service truth kind: {truth.kind}"
            )
    return "\n".join(chunks).strip()


def compose_service_info_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Render verified service information without model-authored factual claims."""
    return (
        deterministic_service_info_reply(
            contract,
            arabic=_latest_customer_is_arabic(history),
        ),
        "deterministic:service-information-contract",
    )
