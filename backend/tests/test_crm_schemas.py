import pytest
from pydantic import ValidationError

from app.schemas.crm import (
    ConversationRead,
    MessageCreate,
    PatientCreate,
    PatientUpdate,
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


def test_manually_entered_patient_phone_must_be_exactly_11_digits() -> None:
    patient = PatientCreate(first_name="Adam", phone="01012345678")
    assert patient.phone == "01012345678"

    for invalid in ("0101234567", "010123456789", "+201012345678"):
        with pytest.raises(ValidationError, match="exactly 11 digits"):
            PatientCreate(first_name="Adam", phone=invalid)

    with pytest.raises(ValidationError, match="exactly 11 digits"):
        PatientUpdate(phone="0101234567")


def test_general_phone_normalizer_stays_flexible_for_channels_and_imports() -> None:
    display, normalized = normalize_phone("+20 100-123-4567")
    assert display == "+20 100-123-4567"
    assert normalized == "+201001234567"


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
