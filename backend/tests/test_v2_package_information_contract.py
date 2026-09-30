from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import responder
from app.agents.v2.package_composer import (
    deterministic_package_contract_reply,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import OutcomeChoice, TurnOutcome
from app.services.agent_v2.outcome_builder import _package_information_response_facts
from app.services.agent_v2.response_contract import (
    build_customer_response_contract,
    is_pure_supported_package_contract,
)

NOW = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


def _owned(
    *,
    name: str = "Laser 6 Sessions",
    remaining: int = 3,
    status: str = "active",
    device: str | None = "Candela Gentle",
    expires_at: str | None = "2027-01-31",
) -> dict[str, object]:
    row: dict[str, object] = {
        "name": name,
        "sessions_purchased": 6,
        "sessions_reserved": 1,
        "sessions_consumed": 2,
        "sessions_remaining": remaining,
        "status": "active",
        "effective_status": status,
        "source": "staff",
    }
    if device is not None:
        row["laser_device_name"] = device
    if expires_at is not None:
        row["expires_at"] = expires_at
    return row


def _offer(
    *,
    service: str = "Laser Underarm",
    sessions: int = 6,
    device: str | None = "Prime Lase",
) -> dict[str, object]:
    row: dict[str, object] = {
        "service_name": service,
        "sessions_count": sessions,
        "is_active": True,
        "price": "3000.00 EGP",
        "currency": "EGP",
        "savings": "300.00 EGP",
    }
    if device is not None:
        row["device_name"] = device
    return row


def _package_outcome(
    *,
    owned: list[dict[str, object]] | None = None,
    offers: list[dict[str, object]] | None = None,
) -> TurnOutcome:
    facts: dict[str, object] = {}
    if owned is not None:
        facts["customer_packages"] = {"packages": owned}
    if offers is not None:
        facts["package_offers"] = {"offers": offers}
    return TurnOutcome(
        status="answered",
        response_goal="package_information",
        facts=facts,
    )


def test_owned_package_truth_preserves_exact_identity_sessions_device_status_and_expiry() -> None:
    outcome = _package_outcome(owned=[_owned()])
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].package_truth

    assert truth is not None
    assert truth.owned_requested is True
    assert truth.offers_requested is False
    assert truth.owned_complete_set is True
    assert len(truth.owned_packages) == 1
    package = truth.owned_packages[0]
    assert package.name == "Laser 6 Sessions"
    assert package.device_name == "Candela Gentle"
    assert package.sessions_purchased == 6
    assert package.sessions_remaining == 3
    assert package.effective_status == "active"
    assert package.expires_at == "2027-01-31"
    assert is_pure_supported_package_contract(contract) is True


def test_package_offer_truth_preserves_service_device_sessions_without_commercial_price() -> None:
    outcome = _package_outcome(offers=[_offer()])
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].package_truth

    assert truth is not None
    assert truth.owned_requested is False
    assert truth.offers_requested is True
    offer = truth.package_offers[0]
    assert offer.service_name == "Laser Underarm"
    assert offer.device_name == "Prime Lase"
    assert offer.sessions_count == 6

    payload = truth.model_dump_json()
    assert "3000" not in payload
    assert "EGP" not in payload
    assert "price" not in payload
    assert "savings" not in payload


def test_owned_and_offer_sets_stay_structurally_distinct() -> None:
    outcome = _package_outcome(
        owned=[_owned(name="My Laser Package", remaining=2)],
        offers=[_offer(service="Hydrafacial", sessions=4, device=None)],
    )
    contract = build_customer_response_contract([outcome])
    truth = contract.units[0].package_truth

    assert truth is not None
    assert truth.owned_requested is True
    assert truth.offers_requested is True
    assert truth.owned_packages[0].name == "My Laser Package"
    assert truth.owned_packages[0].sessions_remaining == 2
    assert truth.package_offers[0].service_name == "Hydrafacial"
    assert truth.package_offers[0].sessions_count == 4


def test_deterministic_renderer_keeps_owned_vs_offer_language_separate() -> None:
    contract = build_customer_response_contract(
        [
            _package_outcome(
                owned=[_owned(name="Owned Six", remaining=3)],
                offers=[_offer(service="Laser Underarm", sessions=8)],
            )
        ]
    )
    text = deterministic_package_contract_reply(contract, arabic=True)

    assert "الباكدجات اللي عندك" in text
    assert "Owned Six" in text
    assert "المتبقي: 3" in text
    assert "عروض الباكدجات المتاحة للشراء" in text
    assert "8 جلسة" in text
    assert "3000" not in text
    assert "EGP" not in text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("active", "نشطة"),
        ("expired", "منتهية"),
        ("exhausted", "مستخدمة بالكامل"),
        ("cancelled", "ملغاة"),
    ],
)
def test_effective_status_is_backend_owned_and_not_inferred(
    status: str,
    expected: str,
) -> None:
    contract = build_customer_response_contract(
        [_package_outcome(owned=[_owned(status=status)])]
    )
    text = deterministic_package_contract_reply(contract, arabic=True)

    assert expected in text


def test_empty_owned_set_does_not_turn_offer_into_ownership() -> None:
    contract = build_customer_response_contract(
        [_package_outcome(owned=[], offers=[_offer()])]
    )
    text = deterministic_package_contract_reply(contract, arabic=True)

    assert "مفيش باكدجات مملوكة" in text
    assert "عروض الباكدجات المتاحة للشراء" in text
    assert "Prime Lase" in text


def test_duplicate_visible_owned_identity_fails_closed_without_internal_id() -> None:
    contract = build_customer_response_contract(
        [_package_outcome(owned=[_owned(), _owned()])]
    )
    text = deterministic_package_contract_reply(contract, arabic=True)

    assert "تمييز إضافي" in text
    assert "UUID" not in text
    assert "package_id" not in text


def test_duplicate_visible_offer_identity_fails_closed_without_collapsing() -> None:
    contract = build_customer_response_contract(
        [_package_outcome(offers=[_offer(), _offer()])]
    )
    text = deterministic_package_contract_reply(contract, arabic=True)

    assert "تمييز إضافي" in text


def test_explicit_single_package_scope_renders_only_that_verified_package() -> None:
    contract = build_customer_response_contract(
        [_package_outcome(owned=[_owned(name="Package A", remaining=4, device=None)])]
    )
    text = deterministic_package_contract_reply(contract, arabic=True)

    assert "Package A" in text
    assert "المتبقي: 4" in text
    assert "Package B" not in text


def test_shared_ask_package_choice_goal_is_not_migrated_by_phase_3d() -> None:
    outcome = TurnOutcome(
        status="needs_input",
        response_goal="ask_package_choice",
        facts={"needed": "package"},
        choices=[
            OutcomeChoice(ref="package-choice-1", label="Package A"),
            OutcomeChoice(ref="package-choice-2", label="Package B"),
        ],
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].package_truth is None
    assert is_pure_supported_package_contract(contract) is False


def test_package_purchase_terminal_is_not_owned_by_package_information_contract() -> None:
    outcome = TurnOutcome(
        status="completed",
        response_goal="package_purchased",
        facts={"package_name": "Package A"},
        action_result={"ok": True, "action": "buy_package"},
    )
    contract = build_customer_response_contract([outcome])

    assert contract.units[0].package_truth is None
    assert is_pure_supported_package_contract(contract) is False


def test_pure_package_information_bypasses_generic_responder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic responder must be unreachable")
        ),
    )
    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="فاضلي كام جلسة في الباكدج؟")],
        outcomes=[_package_outcome(owned=[_owned(remaining=3)])],
    )

    assert source == "deterministic:package-contract"
    assert "المتبقي: 3" in text



def test_mixed_package_and_unsupported_family_preserves_verified_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        _package_outcome(owned=[_owned()]),
        TurnOutcome(
            status="answered",
            response_goal="answer_service",
            facts={"service_catalog": {"service": {"name": "Hydrafacial"}}},
        ),
    ]
    monkeypatch.setattr(
        responder,
        "build_realtime_composer_model",
        lambda: (_ for _ in ()).throw(
            AssertionError("mixed typed package response must not invoke the generic model")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="قولّي باكدجاتي والخدمة")],
        outcomes=outcomes,
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "Laser 6 Sessions" in text
    assert "3" in text

def test_package_information_response_facts_remove_phase3b_commercial_fields() -> None:
    shaped = _package_information_response_facts(
        {
            "package_offers": {
                "offers": [
                    {
                        "service_name": "Laser Underarm",
                        "device_name": "Prime Lase",
                        "sessions_count": 6,
                        "is_active": True,
                        "price": "3000.00 EGP",
                        "currency": "EGP",
                        "savings": "300.00 EGP",
                        "standalone_session_price": "550.00 EGP",
                    }
                ]
            }
        }
    )
    offer = shaped["package_offers"]["offers"][0]

    assert offer == {
        "service_name": "Laser Underarm",
        "device_name": "Prime Lase",
        "sessions_count": 6,
        "is_active": True,
    }
