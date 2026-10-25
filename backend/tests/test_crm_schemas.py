import pytest
from pydantic import ValidationError

from app.schemas.crm import (
    ConversationRead,
    MessageCreate,
    PatientCreate,
    PatientRegistrationCreate,
    normalize_patient_identity_phone,
    normalize_phone,
)


def test_phone_normalization_accepts_common_formatting() -> None:
    display, normalized = normalize_phone("+20 100-123-4567")
    assert display == "+20 100-123-4567"
    assert normalized == "+201001234567"




def test_phone_normalization_canonicalizes_egyptian_mobile_variants() -> None:
    variants = [
        "01012345678",
        "+20 101 234 5678",
        "00201012345678",
        "201012345678",
    ]
    normalized = {normalize_patient_identity_phone(value)[1] for value in variants}
    assert normalized == {"+201012345678"}

def test_patient_rejects_invalid_phone() -> None:
    with pytest.raises(ValidationError):
        PatientCreate(first_name="Adam", phone="abc")


def test_patient_message_must_be_inbound() -> None:
    with pytest.raises(ValidationError):
        MessageCreate(
            sender_type="patient",
            direction="outbound",
            content="Hello",
        )


def test_text_message_requires_content() -> None:
    with pytest.raises(ValidationError):
        MessageCreate(sender_type="staff", direction="outbound", content="   ")


def test_conversation_read_requires_phase4_ownership_fields() -> None:
    required = {"owner_type", "unread_count", "ownership_changed_at"}
    assert required.issubset(ConversationRead.model_fields)


@pytest.mark.parametrize("phone", ["01012345678", "01112345678", "01212345678", "01512345678"])
def test_reception_registration_accepts_supported_egyptian_mobile_prefixes(phone: str) -> None:
    patient = PatientRegistrationCreate(first_name="Ada", phone=phone)
    assert patient.phone == phone


@pytest.mark.parametrize("phone", ["01312345678", "0101234567", "010123456789"])
def test_reception_registration_rejects_unsupported_or_invalid_mobile_numbers(phone: str) -> None:
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(first_name="Ada", phone=phone)


def test_reception_registration_requires_package_offer_and_remaining_sessions_together() -> None:
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_package_offer_id="00000000-0000-0000-0000-000000000001",
        )


def test_reception_registration_requires_pulse_device_and_remaining_pulses_together() -> None:
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_pulse_balances=[{"device_key": "candela"}],
        )


def test_reception_registration_accepts_multiple_distinct_pulse_balances() -> None:
    patient = PatientRegistrationCreate(
        first_name="Ada",
        phone="01512345678",
        initial_pulse_balances=[
            {"device_key": "candela", "pulses_remaining": 500},
            {"device_key": "alma", "pulses_remaining": 750},
        ],
    )
    assert [(item.device_key, item.pulses_remaining) for item in patient.initial_pulse_balances] == [
        ("candela", 500),
        ("alma", 750),
    ]


def test_reception_registration_rejects_duplicate_pulse_device_balances() -> None:
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_pulse_balances=[
                {"device_key": "Candela", "pulses_remaining": 500},
                {"device_key": "candela", "pulses_remaining": 250},
            ],
        )


def test_reception_registration_rejects_payment_without_package_offer() -> None:
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_package_amount_paid_minor=100,
        )


def test_reception_registration_preserves_optional_customer_id() -> None:
    patient = PatientRegistrationCreate(
        first_name="Ada",
        phone="01012345678",
        customer_id="000012",
    )
    assert patient.customer_id == "000012"
    assert patient.initial_pulse_balances == []
