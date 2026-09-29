from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.channel_adapter import (
    channel_to_patient_source,
    generate_adapter_token,
    hash_adapter_token,
)
from app.schemas.channel import (
    ChannelConnectionCreate,
    DispatchResultRequest,
    NormalizedInboundMessage,
)


def test_adapter_token_is_only_stored_as_hash() -> None:
    token, token_hash = generate_adapter_token()
    assert token.startswith("tia_ch_")
    assert token not in token_hash
    assert len(token_hash) == 64
    assert hash_adapter_token(token) == token_hash


def test_connection_config_rejects_secrets() -> None:
    with pytest.raises(ValidationError):
        ChannelConnectionCreate(
            channel="whatsapp",
            provider="n8n",
            display_name="Linka WhatsApp",
            config={"access_token": "should-not-be-stored-here"},
        )


def test_connection_config_rejects_nested_secrets() -> None:
    with pytest.raises(ValidationError):
        ChannelConnectionCreate(
            channel="whatsapp",
            provider="n8n",
            display_name="Linka WhatsApp",
            config={"provider": {"api_key": "nope"}},
        )


def test_connection_config_allows_non_secret_provider_metadata() -> None:
    payload = ChannelConnectionCreate(
        channel="whatsapp",
        provider="n8n",
        display_name="Linka WhatsApp",
        external_account_id="201000000000",
        config={"phone_number_id": "123456", "region": "EG"},
    )
    assert payload.config["phone_number_id"] == "123456"


def test_normalized_inbound_strips_external_fields() -> None:
    payload = NormalizedInboundMessage(
        external_event_id=" evt-1 ",
        external_message_id=" msg-1 ",
        external_user_id=" user-1 ",
        text="  عايزة احجز ليزر  ",
    )
    assert payload.external_event_id == "evt-1"
    assert payload.text == "عايزة احجز ليزر"


def test_failed_dispatch_can_request_retry() -> None:
    payload = DispatchResultRequest(
        status="failed",
        error="provider timeout",
        retry_after_seconds=60,
    )
    assert payload.retry_after_seconds == 60


def test_non_failed_dispatch_cannot_request_retry() -> None:
    with pytest.raises(ValidationError):
        DispatchResultRequest(
            status="sent",
            provider_message_id=str(uuid4()),
            retry_after_seconds=60,
        )


def test_channel_source_mapping() -> None:
    assert channel_to_patient_source("whatsapp") == "whatsapp"
    assert channel_to_patient_source("web") == "website"
    assert channel_to_patient_source("sms") == "other"
