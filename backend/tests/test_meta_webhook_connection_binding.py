from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes import whatsapp_setup
from app.models.channel_connection import ChannelConnection
from app.models.channel_provider_credential import ChannelProviderCredential
from app.models.workspace import Workspace
from app.services import meta_whatsapp_transport as transport
from app.services.workspace_runtime_policy import DEMO_WHATSAPP_REPLY_TEST_FLAG

_APP_SECRET = "synthetic-meta-app-secret"


def _connection(
    phone_number_id: str,
    *,
    workspace_id=None,
    config_phone_number_id: str | None = None,
    is_demo_test_number: bool = False,
):
    config = {
        "phone_number_id": config_phone_number_id or phone_number_id,
        "waba_id": "synthetic-waba",
    }
    if is_demo_test_number:
        config[DEMO_WHATSAPP_REPLY_TEST_FLAG] = True
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        channel="whatsapp",
        provider="meta_cloud",
        status="active",
        external_account_id=phone_number_id,
        config_json=config,
    )


def _workspace(*, workspace_id=None, is_demo: bool = False):
    return SimpleNamespace(id=workspace_id or uuid4(), is_demo=is_demo)


def _value(
    phone_number_id: str | None,
    *,
    messages: list[dict] | None = None,
    statuses: list[dict] | None = None,
):
    value = {"metadata": {}}
    if phone_number_id is not None:
        value["metadata"]["phone_number_id"] = phone_number_id
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return value


def _text_message(message_id: str = "wamid.inbound") -> dict:
    return {
        "id": message_id,
        "from": "201001112223",
        "type": "text",
        "text": {"body": "Hello"},
    }


def _delivery_status(message_id: str = "wamid.outbound") -> dict:
    return {
        "id": message_id,
        "status": "delivered",
        "timestamp": "1790000000",
        "recipient_id": "201001112223",
    }


def _payload(*values: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "synthetic-waba",
                "changes": [{"field": "messages", "value": value} for value in values],
            }
        ],
    }


def _signed_body(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, f"sha256={digest}"


def _request(body: bytes) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/channels/whatsapp/webhook/synthetic",
            "raw_path": b"/api/v1/channels/whatsapp/webhook/synthetic",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


class _FakeDB:
    def __init__(self, connection, workspace) -> None:
        self.rows = {
            ChannelConnection: connection,
            ChannelProviderCredential: SimpleNamespace(
                app_secret_ciphertext="synthetic-ciphertext"
            ),
            Workspace: workspace,
        }
        self.get_calls = []

    def get(self, model, _key):
        self.get_calls.append(model)
        return self.rows.get(model)


def _route_harness(monkeypatch, connection, workspace):
    db = _FakeDB(connection, workspace)
    calls = []

    def ingest_media(_db, *, connection, payload):
        calls.append(("media", connection, payload))
        return {"accepted_media": 0, "duplicate_media": 0, "ignored_media": 0}

    def ingest_messages(_db, *, connection, payload):
        calls.append(("messages", connection, payload))
        return {"accepted_inbound": 1, "delivery_statuses": 1, "ignored": 0}

    monkeypatch.setattr(
        whatsapp_setup, "decrypt_provider_secret", lambda _ciphertext: _APP_SECRET
    )
    monkeypatch.setattr(whatsapp_setup, "ingest_meta_media_webhook", ingest_media)
    monkeypatch.setattr(whatsapp_setup, "ingest_meta_webhook", ingest_messages)

    def invoke(payload, *, signature=None):
        body, valid_signature = _signed_body(payload)
        return asyncio.run(
            whatsapp_setup.whatsapp_meta_webhook_receive(
                connection.id,
                _request(body),
                db,
                signature if signature is not None else valid_signature,
            )
        )

    return db, calls, invoke


def test_valid_same_connection_webhook_uses_the_url_authenticated_connection(
    monkeypatch,
):
    connection = _connection("phone-a")
    workspace = _workspace(workspace_id=connection.workspace_id)
    db, calls, invoke = _route_harness(monkeypatch, connection, workspace)

    result = invoke(
        _payload(
            _value(
                "phone-a",
                messages=[_text_message()],
                statuses=[_delivery_status()],
            )
        )
    )

    assert result["received"] is True
    assert result["accepted_inbound"] == 1
    assert result["delivery_statuses"] == 1
    assert [call[0] for call in calls] == ["media", "messages"]
    assert all(call[1] is connection for call in calls)
    assert Workspace in db.get_calls


def test_cross_workspace_mismatch_rejects_entire_batch_before_any_ingestion(
    monkeypatch,
):
    workspace_a = _workspace()
    connection_a = _connection("phone-a", workspace_id=workspace_a.id)
    connection_b = _connection("phone-b")
    assert connection_a.workspace_id != connection_b.workspace_id
    db, calls, invoke = _route_harness(monkeypatch, connection_a, workspace_a)

    payload = _payload(
        _value("phone-a", messages=[_text_message()]),
        _value("phone-b", statuses=[_delivery_status()]),
    )
    with pytest.raises(HTTPException) as rejected:
        invoke(payload)

    assert rejected.value.status_code == 403
    assert calls == []
    assert Workspace not in db.get_calls


def test_different_connection_in_same_workspace_is_not_authorized(monkeypatch):
    workspace = _workspace()
    connection_a = _connection("phone-a", workspace_id=workspace.id)
    connection_b = _connection("phone-b", workspace_id=workspace.id)
    db, calls, invoke = _route_harness(monkeypatch, connection_a, workspace)

    with pytest.raises(HTTPException) as rejected:
        invoke(_payload(_value("phone-b", messages=[_text_message()])))

    assert rejected.value.status_code == 403
    assert calls == []
    assert Workspace not in db.get_calls
    assert connection_a.id != connection_b.id


def test_invalid_signature_is_rejected_before_connection_binding_or_ingestion(
    monkeypatch,
):
    connection = _connection("phone-a")
    workspace = _workspace(workspace_id=connection.workspace_id)
    db, calls, invoke = _route_harness(monkeypatch, connection, workspace)

    with pytest.raises(HTTPException) as rejected:
        invoke(
            _payload(_value("phone-a", messages=[_text_message()])),
            signature="sha256=invalid",
        )

    assert rejected.value.status_code == 401
    assert calls == []
    assert Workspace not in db.get_calls


def test_demo_runtime_policy_applies_to_the_bound_connection(monkeypatch):
    workspace = _workspace(is_demo=True)
    connection = _connection("phone-demo", workspace_id=workspace.id)
    db, calls, invoke = _route_harness(monkeypatch, connection, workspace)

    ignored = invoke(_payload(_value("phone-demo", messages=[_text_message()])))
    assert ignored == {"received": True, "ignored": True}
    assert calls == []

    connection.config_json[DEMO_WHATSAPP_REPLY_TEST_FLAG] = True
    accepted = invoke(_payload(_value("phone-demo", messages=[_text_message()])))
    assert accepted["received"] is True
    assert [call[0] for call in calls] == ["media", "messages"]
    assert all(call[1] is connection for call in calls)


def test_ingestion_service_uses_authenticated_connection_for_messages_and_statuses(
    monkeypatch,
):
    connection = _connection("phone-a")
    calls = []

    def accept(_db, *, connection, payload):
        calls.append(("inbound", connection, payload.external_message_id))

    def record(_db, **kwargs):
        calls.append(("status", kwargs["connection"], kwargs["provider_message_id"]))

    monkeypatch.setattr(transport, "accept_normalized_inbound", accept)
    monkeypatch.setattr(transport, "record_provider_status", record)

    result = transport.ingest_meta_webhook(
        object(),
        connection=connection,
        payload=_payload(
            _value(
                "phone-a",
                messages=[_text_message()],
                statuses=[_delivery_status()],
            )
        ),
    )

    assert result == {"accepted_inbound": 1, "delivery_statuses": 1, "ignored": 0}
    assert [call[0] for call in calls] == ["inbound", "status"]
    assert all(call[1] is connection for call in calls)


def test_ingestion_service_rejects_mismatched_batch_before_message_or_status_mutation(
    monkeypatch,
):
    connection_a = _connection("phone-a")
    calls = []

    def unexpected(*_args, **_kwargs):
        calls.append("side-effect")
        pytest.fail("A mismatched webhook must be rejected before persistence.")

    monkeypatch.setattr(transport, "accept_normalized_inbound", unexpected)
    monkeypatch.setattr(transport, "record_provider_status", unexpected)

    with pytest.raises(transport.MetaWhatsAppWebhookBindingError):
        transport.ingest_meta_webhook(
            object(),
            connection=connection_a,
            payload=_payload(
                _value("phone-a", messages=[_text_message()]),
                _value("phone-b", statuses=[_delivery_status()]),
            ),
        )

    assert calls == []


def test_ingestion_service_rejects_events_without_a_phone_number_binding(
    monkeypatch,
):
    connection = _connection("phone-a")
    monkeypatch.setattr(
        transport,
        "accept_normalized_inbound",
        lambda *_args, **_kwargs: pytest.fail("Unbound event reached persistence."),
    )

    with pytest.raises(transport.MetaWhatsAppWebhookBindingError):
        transport.ingest_meta_webhook(
            object(),
            connection=connection,
            payload=_payload(_value(None, messages=[_text_message()])),
        )


def test_inconsistent_connection_phone_configuration_fails_closed():
    connection = _connection(
        "phone-a",
        config_phone_number_id="phone-b",
    )

    with pytest.raises(transport.MetaWhatsAppWebhookBindingError):
        transport.validate_meta_webhook_connection_binding(
            connection,
            _payload(_value("phone-a", messages=[_text_message()])),
        )
