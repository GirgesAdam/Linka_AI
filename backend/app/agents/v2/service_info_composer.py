from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    ServiceInfo,
    ServiceTruth,
    is_pure_supported_service_contract,
)


class ServiceComposerValidationError(RuntimeError):
    pass


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    return " ".join(message.content.strip().split())[:limit]


def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            text = _message_text(message)
            if text:
                return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _detail_reply(truth: ServiceTruth, service: ServiceInfo, *, arabic: bool) -> str:
    parts: list[str] = []
    if arabic:
        parts.append(f"الخدمة: {service.name}.")
    else:
        parts.append(f"Service: {service.name}.")

    for detail in truth.requested_details:
        if detail == "description":
            if service.description:
                parts.append(service.description)
            else:
                parts.append(
                    "لا يوجد وصف إضافي مقدم من العيادة لهذه الخدمة في البيانات الحالية."
                    if arabic
                    else "No additional clinic-provided description is stored for this service."
                )
        elif detail == "duration":
            if service.customer_duration_text:
                parts.append(
                    f"مدة الموعد المعروضة للعميل: {service.customer_duration_text}."
                    if arabic
                    else f"Customer-facing scheduled duration: {service.customer_duration_text}."
                )
            elif service.duration_minutes is not None:
                parts.append(
                    f"مدة الموعد المحددة في النظام: {service.duration_minutes} دقيقة."
                    if arabic
                    else f"Configured appointment duration: {service.duration_minutes} minutes."
                )
            else:
                parts.append(
                    "لا توجد مدة للموعد مسجلة في بيانات العيادة الحالية."
                    if arabic
                    else "No appointment duration is stored in the current clinic data."
                )
        elif detail == "devices":
            if not service.devices_complete_set:
                raise ServiceComposerValidationError("Device association set is not verified complete.")
            if service.device_names:
                rendered = "، ".join(service.device_names) if arabic else ", ".join(service.device_names)
                parts.append(
                    f"الأجهزة المرتبطة بالخدمة: {rendered}."
                    if arabic
                    else f"Devices associated with this service: {rendered}."
                )
            else:
                parts.append(
                    "لا توجد أجهزة مرتبطة بهذه الخدمة في بيانات العيادة الحالية."
                    if arabic
                    else "No devices are associated with this service in the current clinic data."
                )
    return " ".join(parts)


def _render_truth(truth: ServiceTruth, *, arabic: bool) -> str:
    if truth.kind == "service_list":
        if not truth.complete_set:
            raise ServiceComposerValidationError("Service list must be a verified complete set.")
        if not truth.services:
            return (
                "لا توجد خدمات في كتالوج العيادة الحالي."
                if arabic
                else "No services are present in the current clinic catalog."
            )
        names = [item.name for item in truth.services]
        rendered = "، ".join(names) if arabic else ", ".join(names)
        return (
            f"الخدمات الموجودة في كتالوج العيادة: {rendered}."
            if arabic
            else f"Services in the clinic catalog: {rendered}."
        )

    if truth.kind != "service_detail" or len(truth.services) != 1:
        raise ServiceComposerValidationError("Service detail requires exactly one verified service.")
    return _detail_reply(truth, truth.services[0], arabic=arabic)


def deterministic_service_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_service_contract(contract):
        raise ServiceComposerValidationError(
            "Service composer requires a pure supported service-information contract."
        )
    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.service_truth
        if truth is None:
            raise ServiceComposerValidationError("Service truth is missing.")
        chunks.append(_render_truth(truth, arabic=arabic))
    return "\n".join(chunks).strip()


def compose_service_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    return (
        deterministic_service_contract_reply(
            contract,
            arabic=_latest_customer_is_arabic(history),
        ),
        "deterministic:service-information-contract",
    )
