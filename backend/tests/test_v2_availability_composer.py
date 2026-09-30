from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agents.v2 import availability_composer, responder
from app.agents.v2.availability_composer import (
    AvailabilityComposerDraft,
    AvailabilityComposerUnitDraft,
    AvailabilityComposerValidationError,
    _build_availability_composer_messages,
    deterministic_availability_fallback,
    resolve_availability_composer_draft,
    validate_availability_composer_draft,
)
from app.agents.v2.responder import compose_v2_customer_reply
from app.services.agent_v2.outcome import TurnOutcome
from app.services.agent_v2.response_contract import (
    CustomerResponseContract,
    build_customer_response_contract,
    is_pure_supported_availability_contract,
)

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)


def _window(
    start: str,
    end: str | None = None,
    *,
    doctor: str = "د. مريم",
    device: str | None = None,
) -> dict[str, object]:
    end_value = end or start
    row: dict[str, object] = {
        "doctor_name": doctor,
        "start_local": f"2026-10-01T{start}:00+03:00",
        "end_local": f"2026-10-01T{end_value}:00+03:00",
        "start_time_24h": start,
        "end_time_24h": end_value,
    }
    if device:
        row["laser_device_name"] = device
    return row


def _present(
    windows: list[dict[str, object]],
    *,
    checked_dates: list[str] | None = None,
    service: str = "Hydrafacial",
    search_truncated: bool = False,
) -> TurnOutcome:
    return TurnOutcome(
        status="answered",
        response_goal="present_availability",
        facts={
            "availability": {
                "service_name": service,
                "checked_dates": checked_dates or ["2026-10-01"],
                "availability_windows": windows,
                "available_option_count": len(windows),
                "search_truncated": search_truncated,
            }
        },
    )


def _exact_miss(
    *,
    time: str = "19:00",
    checked_dates: list[str] | None = None,
    search_truncated: bool = False,
) -> TurnOutcome:
    return TurnOutcome(
        status="blocked",
        response_goal="requested_time_unavailable",
        facts={
            "time": {"mode": "exact", "start_time": time, "end_time": None},
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": checked_dates or ["2026-10-01"],
                "availability_windows": [],
                "available_option_count": 0,
                "search_truncated": search_truncated,
            },
        },
    )


def _no_availability(
    checked_dates: list[str],
    *,
    search_truncated: bool = False,
) -> TurnOutcome:
    return TurnOutcome(
        status="blocked",
        response_goal="no_availability",
        facts={
            "availability": {
                "service_name": "Hydrafacial",
                "checked_dates": checked_dates,
                "availability_windows": [],
                "available_option_count": 0,
                "search_truncated": search_truncated,
            }
        },
    )


def _draft(
    contract: CustomerResponseContract,
    *,
    mode: str = "compact",
    closing: str | None = None,
) -> AvailabilityComposerDraft:
    units = []
    for index, unit in enumerate(contract.units):
        truth = unit.availability_truth
        assert truth is not None
        windows = next(
            (
                fact.value
                for fact in unit.facts
                if fact.key == "availability_windows"
            ),
            [],
        )
        refs = [
            f"unit_{index}_window_{window_index}"
            for window_index, _ in enumerate(windows if isinstance(windows, list) else [])
        ]
        selected_closing = closing or {
            "options_available": "ask_selection",
            "requested_time_unavailable": "offer_other_time",
            "no_availability": "offer_other_scope",
        }[truth.state]
        units.append(
            AvailabilityComposerUnitDraft(
                unit_index=index,
                availability_ref="unit_availability",
                style="warm",
                window_refs=refs,
                optional_fact_keys=["service_name"]
                if any(fact.key == "service_name" for fact in unit.facts)
                else [],
                presentation_mode=mode,
                closing_action=selected_closing,
                transition="sentence" if index == 0 else "and",
            )
        )
    return AvailabilityComposerDraft(units=units)


def _render(outcomes: list[TurnOutcome], *, mode: str = "compact") -> str:
    contract = build_customer_response_contract(outcomes)
    return resolve_availability_composer_draft(
        contract,
        _draft(contract, mode=mode),
        arabic=True,
    )


def test_present_availability_contract_has_backend_truth_and_complete_windows() -> None:
    contract = build_customer_response_contract([_present([_window("17:00")])])
    unit = contract.units[0]
    windows = next(fact for fact in unit.facts if fact.key == "availability_windows")

    assert is_pure_supported_availability_contract(contract) is True
    assert unit.availability_truth is not None
    assert unit.availability_truth.state == "options_available"
    assert windows.requirement == "required"
    assert windows.complete_set is True


def test_malformed_availability_shapes_do_not_enter_phase3a_cutover() -> None:
    empty_present = build_customer_response_contract(
        [_present([])]
    )
    missing_requested_time = build_customer_response_contract(
        [
            TurnOutcome(
                status="blocked",
                response_goal="requested_time_unavailable",
                facts={
                    "availability": {
                        "checked_dates": ["2026-10-01"],
                        "availability_windows": [],
                    }
                },
            )
        ]
    )

    assert is_pure_supported_availability_contract(empty_present) is False
    assert is_pure_supported_availability_contract(missing_requested_time) is False


def test_exact_miss_projects_requested_time_from_step_facts() -> None:
    contract = build_customer_response_contract([_exact_miss(time="19:00")])
    unit = contract.units[0]
    facts = {fact.key: fact for fact in unit.facts}

    assert unit.availability_truth is not None
    assert unit.availability_truth.state == "requested_time_unavailable"
    assert facts["requested_time"].value == {
        "mode": "exact",
        "start_time": "19:00",
    }
    assert facts["requested_time"].requirement == "required"


def test_no_availability_projects_search_scope_and_truncation() -> None:
    contract = build_customer_response_contract(
        [_no_availability(["2026-10-01", "2026-10-02"], search_truncated=True)]
    )
    facts = {fact.key: fact for fact in contract.units[0].facts}

    assert facts["checked_dates"].value == ["2026-10-01", "2026-10-02"]
    assert facts["search_truncated"].value is True


@pytest.mark.parametrize(
    "windows",
    [
        [_window("17:00")],
        [_window("17:00"), _window("19:00")],
        [_window("17:00", doctor="د. مريم"), _window("19:00", doctor="د. سارة")],
        [
            _window("17:00", device="Prime Lase"),
            _window("19:00", device="Candela Gentle"),
        ],
        [
            _window("17:00", doctor="د. مريم", device="Prime Lase"),
            _window("19:00", doctor="د. سارة", device="Candela Gentle"),
        ],
    ],
)
def test_present_availability_renders_every_verified_window(
    windows: list[dict[str, object]],
) -> None:
    text = _render([_present(windows)])

    for window in windows:
        start = str(window["start_time_24h"])
        expected_hour = str(int(start[:2]) % 12 or 12)
        assert expected_hour in text
        if window.get("doctor_name"):
            assert str(window["doctor_name"]) in text
        if window.get("laser_device_name"):
            assert str(window["laser_device_name"]) in text


def test_multiple_dates_are_backend_resolved() -> None:
    windows = [
        _window("17:00"),
        {
            **_window("19:00"),
            "start_local": "2026-10-02T19:00:00+03:00",
            "end_local": "2026-10-02T19:00:00+03:00",
        },
    ]
    text = _render(
        [_present(windows, checked_dates=["2026-10-01", "2026-10-02"])],
        mode="detailed",
    )

    assert "1 أكتوبر 2026" in text
    assert "2 أكتوبر 2026" in text


def test_continuous_verified_range_is_described_as_bookable_starts_not_duration() -> None:
    text = _render([_present([_window("17:00", "19:00")])])

    assert "بدايات حجز من 5 مساءً لـ7 مساءً" in text
    assert "الجلسة من" not in text


def test_non_contiguous_windows_are_not_gap_filled() -> None:
    text = _render([_present([_window("17:00"), _window("19:00")])])

    assert "الساعة 5 مساءً" in text
    assert "الساعة 7 مساءً" in text
    assert "بدايات حجز من 5 مساءً لـ7 مساءً" not in text


def test_single_start_only_window_remains_single_start() -> None:
    text = _render([_present([_window("17:00")])])

    assert "الساعة 5 مساءً" in text
    assert "بدايات حجز" not in text


def test_complete_set_cannot_be_silently_truncated() -> None:
    contract = build_customer_response_contract(
        [_present([_window("17:00"), _window("19:00")])]
    )
    draft = _draft(contract)
    draft.units[0].window_refs.pop()

    with pytest.raises(
        AvailabilityComposerValidationError,
        match="complete verified window set",
    ):
        validate_availability_composer_draft(contract, draft)


def test_compound_availability_units_preserve_order_and_namespaces() -> None:
    contract = build_customer_response_contract(
        [
            _present([_window("17:00", doctor="د. مريم")]),
            _present([_window("19:00", doctor="د. سارة")]),
        ]
    )
    draft = _draft(contract)

    validate_availability_composer_draft(contract, draft)
    text = resolve_availability_composer_draft(contract, draft, arabic=True)
    assert text.index("د. مريم") < text.index("د. سارة")


def test_requested_exact_time_unavailable_is_explicit_and_backend_resolved() -> None:
    text = _render([_exact_miss(time="19:00")])

    assert "الساعة 7 مساءً" in text
    assert "مش متاح" in text
    assert "1 أكتوبر 2026" in text


def test_runtime_exact_miss_has_no_same_outcome_alternatives() -> None:
    contract = build_customer_response_contract([_exact_miss()])
    unit = contract.units[0]

    assert all(fact.key != "availability_windows" for fact in unit.facts)


def test_exact_miss_plus_separate_verified_alternative_unit_keeps_both_semantics() -> None:
    text = _render(
        [
            _exact_miss(time="19:00"),
            _present([_window("20:00")]),
        ]
    )

    assert text.index("مش متاح") < text.index("8 مساءً")
    assert "8 مساءً" in text
    assert "أقدر أشوفلك وقت تاني" not in text


@pytest.mark.parametrize(
    ("checked_dates", "truncated", "expected"),
    [
        (["2026-10-01"], False, "يوم 1 أكتوبر 2026"),
        (
            ["2026-10-01", "2026-10-02", "2026-10-03"],
            False,
            "من 1 أكتوبر 2026 لحد 3 أكتوبر 2026",
        ),
        (
            ["2026-10-01", "2026-10-03"],
            False,
            "في الأيام اللي اتفحصت",
        ),
        (
            ["2026-10-01", "2026-10-02"],
            True,
            "بس نطاق البحث اللي اتفحص",
        ),
    ],
)
def test_no_availability_is_scoped_to_checked_search(
    checked_dates: list[str],
    truncated: bool,
    expected: str,
) -> None:
    text = _render(
        [_no_availability(checked_dates, search_truncated=truncated)]
    )

    assert expected in text
    assert "مفيش مواعيد خالص" not in text
    assert "مفيش أي مواعيد مستقبلية" not in text


def test_next_available_bounded_zero_search_stays_scoped() -> None:
    dates = [f"2026-10-{day:02d}" for day in range(1, 15)]
    text = _render([_no_availability(dates, search_truncated=True)])

    assert "من 1 أكتوبر 2026 لحد 14 أكتوبر 2026" in text
    assert "بس نطاق البحث اللي اتفحص" in text


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "duplicate", "cross_unit", "wrong_unit", "unknown_fact"],
)
def test_invalid_symbolic_references_fail_structural_validation(mutation: str) -> None:
    contract = build_customer_response_contract(
        [
            _present([_window("17:00")]),
            _present([_window("19:00")]),
        ]
    )
    draft = _draft(contract)

    if mutation == "unknown":
        draft.units[0].window_refs = ["unit_0_window_99"]
    elif mutation == "duplicate":
        draft.units[0].window_refs = ["unit_0_window_0", "unit_0_window_0"]
    elif mutation == "cross_unit":
        draft.units[0].window_refs = ["unit_1_window_0"]
    elif mutation == "wrong_unit":
        draft.units[0].unit_index = 1
    else:
        draft.units[0].optional_fact_keys = ["doctor_name"]

    with pytest.raises(AvailabilityComposerValidationError):
        validate_availability_composer_draft(contract, draft)


def test_composer_contract_view_contains_no_exact_availability_values() -> None:
    contract = build_customer_response_contract(
        [
            _present(
                [
                    _window(
                        "17:00",
                        doctor="Dr Mary",
                        device="Prime Lase",
                    )
                ],
                checked_dates=["2026-10-01"],
                service="Laser",
            )
        ]
    )
    messages = _build_availability_composer_messages(
        history=[
            HumanMessage(
                content="عايز الساعة 5 يوم 1 أكتوبر مع Dr Mary على Prime Lase"
            )
        ],
        contract=contract,
    )
    payload = "\n".join(str(message.content) for message in messages)

    assert "unit_0_window_0" in payload
    assert "17:00" not in payload
    assert "2026-10-01" not in payload
    assert "Dr Mary" not in payload
    assert "Prime Lase" not in payload
    assert "Laser" not in payload


def test_invalid_model_draft_falls_back_without_legacy_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _present([_window("17:00")])
    monkeypatch.setattr(
        availability_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        availability_composer,
        "model_label",
        lambda name: str(name),
    )
    monkeypatch.setattr(
        availability_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(
            value=AvailabilityComposerDraft(
                units=[
                    AvailabilityComposerUnitDraft(
                        unit_index=0,
                        availability_ref="unit_availability",
                        style="warm",
                        window_refs=["unit_0_window_99"],
                        optional_fact_keys=[],
                        presentation_mode="compact",
                        closing_action="ask_selection",
                        transition="sentence",
                    )
                ]
            ),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr(
        responder,
        "_deterministic_availability_guard_reply",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy availability guard must be unreachable")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="إيه المتاح؟")],
        outcomes=[outcome],
    )

    assert source == "deterministic:availability-contract-fallback"
    assert "5 مساءً" in text


def test_valid_availability_path_never_calls_legacy_responder_or_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _present([_window("17:00")])
    contract = build_customer_response_contract([outcome])
    draft = _draft(contract)
    monkeypatch.setattr(
        availability_composer,
        "build_realtime_composer_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        availability_composer,
        "model_label",
        lambda name: str(name),
    )
    monkeypatch.setattr(
        availability_composer,
        "invoke_with_model_chain",
        lambda **_kwargs: SimpleNamespace(value=draft, model_name="test-model"),
    )
    monkeypatch.setattr(
        responder,
        "_build_responder_messages",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy responder must be unreachable")
        ),
    )
    monkeypatch.setattr(
        responder,
        "_deterministic_availability_guard_reply",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy availability guard must be unreachable")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="إيه المتاح؟")],
        outcomes=[outcome],
    )

    assert source == "availability-contract:test-model"
    assert "5 مساءً" in text



def test_mixed_availability_and_unsupported_family_preserves_verified_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [
        _present([_window("17:00")]),
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
            AssertionError("mixed typed availability must not invoke the generic model")
        ),
    )

    text, source = compose_v2_customer_reply(
        clinic_name="Linka Clinic",
        timezone_name="Africa/Cairo",
        local_now=NOW,
        history=[HumanMessage(content="إيه المتاح وتفاصيل الخدمة؟")],
        outcomes=outcomes,
    )

    assert source == "deterministic:mixed-typed-contract"
    assert "5 مساء" in text
    assert "legacy" not in text

def test_fallback_preserves_all_windows_and_no_gap_expansion() -> None:
    contract = build_customer_response_contract(
        [_present([_window("17:00"), _window("19:00")])]
    )
    text = deterministic_availability_fallback(contract, arabic=True)

    assert "5 مساءً" in text
    assert "7 مساءً" in text
    assert "بدايات حجز من 5 مساءً لـ7 مساءً" not in text
