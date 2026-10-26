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


def test_reception_registration_validates_package_rows_and_rejects_duplicate_offer() -> None:
    offer_id = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_packages=[{"offer_id": offer_id}],
        )
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_packages=[
                {"offer_id": offer_id, "sessions_remaining": 2},
                {"offer_id": offer_id, "sessions_remaining": 1},
            ],
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


def test_reception_registration_accepts_multiple_package_financial_rows() -> None:
    patient = PatientRegistrationCreate(
        first_name="Ada",
        phone="01012345678",
        initial_packages=[
            {
                "offer_id": "00000000-0000-0000-0000-000000000001",
                "sessions_remaining": 2,
                "amount_paid_minor": 100_000,
                "payment_method": "cash",
            },
            {
                "offer_id": "00000000-0000-0000-0000-000000000002",
                "sessions_remaining": 3,
                "amount_paid_minor": 250_000,
                "payment_method": "visa",
            },
        ],
    )
    assert len(patient.initial_packages) == 2
    assert patient.initial_packages[0].amount_paid_minor == 100_000
    assert patient.initial_packages[1].payment_method == "visa"


def test_reception_registration_preserves_optional_customer_id() -> None:
    patient = PatientRegistrationCreate(
        first_name="Ada",
        phone="01012345678",
        customer_id="000012",
    )
    assert patient.customer_id == "000012"
    assert patient.initial_pulse_balances == []

def test_reception_registration_rejects_removed_singular_package_fields() -> None:
    with pytest.raises(ValidationError):
        PatientRegistrationCreate(
            first_name="Ada",
            phone="01012345678",
            initial_package_offer_id="00000000-0000-0000-0000-000000000001",
            initial_package_sessions_remaining=2,
        )
