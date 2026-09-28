from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    PatientTruth,
    is_pure_supported_patient_contract,
)


class PatientComposerValidationError(RuntimeError):
    pass


def _message_text(message: BaseMessage, *, limit: int = 1000) -> str:
    if not isinstance(message.content, str) or not message.content.strip():
        return ""
    text = " ".join(message.content.strip().split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _latest_customer_is_arabic(history: list[BaseMessage]) -> bool:
    for message in reversed(history):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message)
        if text:
            return any("\u0600" <= char <= "\u06ff" for char in text)
    return False


def _display_name(truth: PatientTruth) -> str:
    if not truth.first_name:
        raise PatientComposerValidationError("Requested patient name is missing.")
    parts = [truth.first_name]
    if truth.last_name:
        parts.append(truth.last_name)
    return " ".join(parts)


def _language_label(value: str, *, arabic: bool) -> str:
    normalized = value.strip().casefold()
    if normalized == "ar":
        return "العربية" if arabic else "Arabic"
    if normalized == "en":
        return "الإنجليزية" if arabic else "English"
    return value


def deterministic_patient_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_patient_contract(contract):
        raise PatientComposerValidationError(
            "Patient composer requires a pure supported patient-profile contract."
        )

    rendered: list[str] = []
    for unit in contract.units:
        truth = unit.patient_truth
        if truth is None:
            raise PatientComposerValidationError("Patient truth is missing.")

        parts: list[str] = []
        for detail in truth.requested_details:
            if detail == "name":
                name = _display_name(truth)
                parts.append(
                    f"الاسم المسجل: {name}" if arabic else f"Name on file: {name}"
                )
            elif detail == "phone":
                if truth.phone:
                    parts.append(
                        f"رقم الموبايل المسجل: {truth.phone}"
                        if arabic
                        else f"Phone on file: {truth.phone}"
                    )
                else:
                    parts.append(
                        "مفيش رقم موبايل مسجل عندنا"
                        if arabic
                        else "There is no phone number on file"
                    )
            elif detail == "preferred_language":
                if not truth.preferred_language:
                    raise PatientComposerValidationError(
                        "Requested preferred language is missing."
                    )
                language = _language_label(
                    truth.preferred_language,
                    arabic=arabic,
                )
                parts.append(
                    f"اللغة المفضلة: {language}"
                    if arabic
                    else f"Preferred language: {language}"
                )
        rendered.append(("، " if arabic else "; ").join(parts) + ".")

    return "\n".join(rendered).strip()


def compose_patient_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Render exact current-patient profile fields without model-authored identity."""
    return (
        deterministic_patient_contract_reply(
            contract,
            arabic=_latest_customer_is_arabic(history),
        ),
        "deterministic:patient-contract",
    )
