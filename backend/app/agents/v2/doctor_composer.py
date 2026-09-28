from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage

from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    DoctorOption,
    is_pure_supported_doctor_contract,
)


class DoctorComposerValidationError(RuntimeError):
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


def _display_option(option: DoctorOption, *, arabic: bool) -> str:
    if not option.specialization:
        return option.name
    if arabic:
        return f"{option.name} — التخصص: {option.specialization}"
    return f"{option.name} — specialization: {option.specialization}"


def _has_ambiguous_duplicate_identity(options: tuple[DoctorOption, ...]) -> bool:
    signatures = [
        (option.name.casefold(), (option.specialization or "").casefold())
        for option in options
    ]
    return len(signatures) != len(set(signatures))


def deterministic_doctor_contract_reply(
    contract: CustomerResponseContract,
    *,
    arabic: bool,
) -> str:
    if not is_pure_supported_doctor_contract(contract):
        raise DoctorComposerValidationError(
            "Doctor composer requires a pure supported doctor contract."
        )

    chunks: list[str] = []
    for unit in contract.units:
        truth = unit.doctor_truth
        if truth is None:
            raise DoctorComposerValidationError("Doctor truth is missing.")

        options = truth.options
        if _has_ambiguous_duplicate_identity(options):
            chunks.append(
                (
                    "في أكتر من دكتور مطابق بنفس الاسم والبيانات الظاهرة، "
                    "فمحتاجين تمييز إضافي من بيانات العيادة قبل الاختيار."
                )
                if arabic
                else (
                    "More than one matching doctor has the same visible name and details, "
                    "so the clinic data needs an additional distinction before choosing."
                )
            )
            continue

        rendered = [_display_option(option, arabic=arabic) for option in options]

        if truth.kind == "doctor_result_set":
            if not rendered:
                chunks.append(
                    "مش لاقية دكاترة مطابقين للسؤال في بيانات العيادة الحالية."
                    if arabic
                    else "No doctors matching the request are verified in the current clinic data."
                )
            elif len(rendered) == 1:
                chunks.append(
                    f"الدكتور المطابق للسؤال: {rendered[0]}."
                    if arabic
                    else f"The doctor matching the request is {rendered[0]}."
                )
            else:
                chunks.append(
                    "الدكاترة المطابقين للسؤال: " + "، ".join(rendered) + "."
                    if arabic
                    else "Doctors matching the request: " + ", ".join(rendered) + "."
                )
            continue

        if truth.kind == "doctor_choice":
            if not rendered:
                raise DoctorComposerValidationError(
                    "Doctor choice truth must contain verified options."
                )
            chunks.append(
                "تقصد مين فيهم: " + "، ".join(rendered) + "؟"
                if arabic
                else "Which one do you mean: " + ", ".join(rendered) + "?"
            )
            continue

        raise DoctorComposerValidationError(
            f"Unsupported doctor truth kind: {truth.kind}"
        )

    return "\n".join(chunks).strip()


def compose_doctor_contract_reply(
    *,
    history: list[BaseMessage],
    contract: CustomerResponseContract,
) -> tuple[str, str]:
    """Render verified doctor identity without any model-authored doctor facts."""
    arabic = _latest_customer_is_arabic(history)
    return (
        deterministic_doctor_contract_reply(contract, arabic=arabic),
        "deterministic:doctor-contract",
    )
