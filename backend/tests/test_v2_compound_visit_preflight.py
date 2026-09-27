from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

from app.integrations.clinic.base import AvailabilityResult, AvailabilitySlot
from app.models.service import Service
from app.services.agent_v2.compound_turn_policy import normalize_compound_turn_plan
from app.services.agent_v2.compound_visit_preflight import preflight_compound_visit_plan
from app.services.agent_v2.planner import PlanStep, ReadRequest, TurnPlan, WriteIntent
from app.services.agent_v2.read_executor import ReadExecutionContext

SERVICE_A = "11111111-1111-1111-1111-111111111111"
SERVICE_B = "22222222-2222-2222-2222-222222222222"
DOCTOR_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DOCTOR_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
BRANCH = "33333333-3333-3333-3333-333333333333"
BRANCH_UUID = UUID(BRANCH)
BRANCH_B = "66666666-6666-6666-6666-666666666666"
WORKSPACE = UUID("44444444-4444-4444-4444-444444444444")
PATIENT = UUID("55555555-5555-5555-5555-555555555555")
DAY = date(2026, 9, 20)


def _booking(
    index: int,
    service_id: str,
    doctor_id: str,
    *,
    branch_id: str | None = BRANCH,
) -> PlanStep:
    params: dict[str, object] = {
        "service_id": service_id,
        "doctor_id": doctor_id,
        "date": {"mode": "exact", "start_date": DAY.isoformat(), "end_date": None},
        "time": {
            "mode": "exact",
            "start_time": "12:00",
            "end_time": None,
            "start_time_ambiguity": "none",
            "end_time_ambiguity": "none",
        },
        "package_usage": "unspecified",
    }
    if branch_id is not None:
        params["branch_id"] = branch_id
    return PlanStep(
        operation_index=index,
        operation_type="book",
        disposition="read",
        reads=[ReadRequest(kind="availability", parameters=dict(params))],
        write_intent=WriteIntent(kind="booking", authorized=True, parameters=dict(params)),
        state_action="start_booking",
        response_goal="present_availability",
        facts={**params, "exact_time_requested": True},
    )


def _slot(
    service_id: str,
    doctor_id: str,
    start_utc: str,
    end_utc: str,
) -> AvailabilitySlot:
    return AvailabilitySlot(
        branch_id=BRANCH,
        branch_name="Clinic",
        doctor_id=doctor_id,
        doctor_name="Doctor",
        service_id=service_id,
        service_name="Service",
        start_at=datetime.fromisoformat(start_utc),
        end_at=datetime.fromisoformat(end_utc),
        duration_minutes=30,
        price_minor=10000,
        currency="EGP",
    )


def _result(service_id: str, slots: list[AvailabilitySlot]) -> AvailabilityResult:
    return AvailabilityResult(
        timezone="Africa/Cairo",
        branch_id=BRANCH,
        branch_name="Clinic",
        service_id=service_id,
        service_name="Service",
        service_duration_minutes=30,
        service_price_minor=10000,
        service_currency="EGP",
        slots=tuple(slots),
    )


class _Adapter:
    def __init__(self, results: dict[tuple[str, date], AvailabilityResult]) -> None:
        self.results = results
        self.requests = []

    def get_availability(self, request):
        self.requests.append(request)
        return self.results.get(
            (request.service_id, request.booking_date),
            _result(request.service_id, []),
        )


class _Db:
    def __init__(self, buffers: dict[str, tuple[int, int]]) -> None:
        self.buffers = buffers

    def get(self, model, key):
        if model is not Service:
            return None
        before, after = self.buffers.get(str(key), (0, 0))
        return SimpleNamespace(
            workspace_id=WORKSPACE,
            buffer_before_minutes=before,
            buffer_after_minutes=after,
        )


def _context(
    adapter: _Adapter,
    *,
    buffers: dict[str, tuple[int, int]] | None = None,
    primary_branch_id: UUID | None = BRANCH_UUID,
    catalog: dict[str, object] | None = None,
) -> ReadExecutionContext:
    return ReadExecutionContext(
        db=_Db(buffers or {}),
        workspace=SimpleNamespace(id=WORKSPACE, primary_branch_id=primary_branch_id),
        patient=SimpleNamespace(id=PATIENT),
        now=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        catalog=catalog,
        adapter=adapter,
    )


def _normalized(
    *,
    same_doctor: bool = False,
    branch_id: str | None = BRANCH,
) -> TurnPlan:
    second_doctor = DOCTOR_A if same_doctor else DOCTOR_B
    return normalize_compound_turn_plan(
        TurnPlan(
            steps=[
                _booking(0, SERVICE_A, DOCTOR_A, branch_id=branch_id),
                _booking(1, SERVICE_B, second_doctor, branch_id=branch_id),
            ]
        ),
        catalog={
            "services": [
                {"id": SERVICE_A, "duration_minutes": 30},
                {"id": SERVICE_B, "duration_minutes": 30},
            ]
        },
    )


def _time(step: PlanStep) -> str:
    source = step.write_intent.parameters if step.write_intent is not None else step.reads[0].parameters
    raw = source["time"]
    assert isinstance(raw, dict)
    return str(raw["start_time"])


def _branch_catalog(*branch_ids: str) -> dict[str, object]:
    return {"branches": [{"id": value, "name": value} for value in branch_ids]}


def _joint_adapter() -> _Adapter:
    return _Adapter(
        {
            (SERVICE_A, DAY): _result(
                SERVICE_A,
                [_slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00")],
            ),
            (SERVICE_B, DAY): _result(
                SERVICE_B,
                [_slot(SERVICE_B, DOCTOR_A, "2026-09-20T09:30:00+00:00", "2026-09-20T10:00:00+00:00")],
            ),
        }
    )


def test_missing_primary_branch_uses_single_catalog_branch() -> None:
    adapter = _joint_adapter()
    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True, branch_id=None),
        context=_context(
            adapter,
            primary_branch_id=None,
            catalog=_branch_catalog(BRANCH),
        ),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent is not None for step in planned.steps] == [True, True]
    assert [request.branch_id for request in adapter.requests] == [BRANCH, BRANCH]
    assert all(
        step.write_intent is not None
        and step.write_intent.parameters["branch_id"] == BRANCH
        for step in planned.steps
    )


def test_explicit_branch_wins_over_primary_branch() -> None:
    adapter = _joint_adapter()
    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True, branch_id=BRANCH),
        context=_context(
            adapter,
            primary_branch_id=UUID(BRANCH_B),
            catalog=_branch_catalog(BRANCH_B),
        ),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent is not None for step in planned.steps] == [True, True]
    assert [request.branch_id for request in adapter.requests] == [BRANCH, BRANCH]


def test_missing_step_branch_uses_workspace_primary_branch() -> None:
    adapter = _joint_adapter()
    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True, branch_id=None),
        context=_context(adapter, primary_branch_id=UUID(BRANCH)),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent is not None for step in planned.steps] == [True, True]
    assert [request.branch_id for request in adapter.requests] == [BRANCH, BRANCH]


def test_missing_primary_with_multiple_catalog_branches_fails_closed() -> None:
    adapter = _joint_adapter()
    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True, branch_id=None),
        context=_context(
            adapter,
            primary_branch_id=None,
            catalog=_branch_catalog(BRANCH, BRANCH_B),
        ),
        timezone_name="Africa/Cairo",
    )

    assert adapter.requests == []
    assert [step.write_intent for step in planned.steps] == [None, None]
    assert all(step.facts["compound_visit_no_joint_window"] is True for step in planned.steps)


def test_missing_primary_with_no_catalog_branches_fails_closed() -> None:
    adapter = _joint_adapter()
    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True, branch_id=None),
        context=_context(
            adapter,
            primary_branch_id=None,
            catalog=_branch_catalog(),
        ),
        timezone_name="Africa/Cairo",
    )

    assert adapter.requests == []
    assert [step.write_intent for step in planned.steps] == [None, None]


def test_resolved_branch_does_not_turn_real_no_availability_into_success() -> None:
    adapter = _Adapter(
        {
            (SERVICE_A, DAY): _result(
                SERVICE_A,
                [_slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00")],
            ),
            (SERVICE_B, DAY): _result(SERVICE_B, []),
        }
    )
    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True, branch_id=None),
        context=_context(
            adapter,
            primary_branch_id=None,
            catalog=_branch_catalog(BRANCH),
        ),
        timezone_name="Africa/Cairo",
    )

    assert [request.branch_id for request in adapter.requests[:2]] == [BRANCH, BRANCH]
    assert [step.write_intent for step in planned.steps] == [None, None]
    assert all(step.facts["compound_visit_no_joint_window"] is True for step in planned.steps)


def test_exact_anchor_executes_only_when_full_joint_window_fits() -> None:
    adapter = _Adapter(
        {
            (SERVICE_A, DAY): _result(
                SERVICE_A,
                [
                    _slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00"),
                    _slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:30:00+00:00", "2026-09-20T10:00:00+00:00"),
                ],
            ),
            (SERVICE_B, DAY): _result(
                SERVICE_B,
                [
                    _slot(SERVICE_B, DOCTOR_A, "2026-09-20T09:30:00+00:00", "2026-09-20T10:00:00+00:00"),
                    _slot(SERVICE_B, DOCTOR_A, "2026-09-20T10:00:00+00:00", "2026-09-20T10:30:00+00:00"),
                ],
            ),
        }
    )

    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True),
        context=_context(adapter),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent is not None for step in planned.steps] == [True, True]
    assert [_time(step) for step in planned.steps] == ["12:00", "12:30"]
    assert all(step.facts["compound_visit_preflight_resolved"] is True for step in planned.steps)
    assert all("compound_visit_sequence_index" not in step.facts for step in planned.steps)


def test_occupied_second_start_suppresses_all_writes_and_offers_next_joint_window() -> None:
    adapter = _Adapter(
        {
            (SERVICE_A, DAY): _result(
                SERVICE_A,
                [
                    _slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00"),
                    _slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:30:00+00:00", "2026-09-20T10:00:00+00:00"),
                ],
            ),
            (SERVICE_B, DAY): _result(
                SERVICE_B,
                [
                    _slot(SERVICE_B, DOCTOR_A, "2026-09-20T10:00:00+00:00", "2026-09-20T10:30:00+00:00"),
                    _slot(SERVICE_B, DOCTOR_A, "2026-09-20T10:15:00+00:00", "2026-09-20T10:45:00+00:00"),
                ],
            ),
        }
    )

    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True),
        context=_context(adapter),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent for step in planned.steps] == [None, None]
    assert [step.disposition for step in planned.steps] == ["read", "read"]
    assert [_time(step) for step in planned.steps] == ["12:30", "13:00"]
    assert all(step.facts["compound_visit_alternative"] is True for step in planned.steps)


def test_same_doctor_service_buffers_are_included_in_joint_window() -> None:
    adapter = _Adapter(
        {
            (SERVICE_A, DAY): _result(
                SERVICE_A,
                [_slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00")],
            ),
            (SERVICE_B, DAY): _result(
                SERVICE_B,
                [_slot(SERVICE_B, DOCTOR_A, "2026-09-20T09:45:00+00:00", "2026-09-20T10:15:00+00:00")],
            ),
        }
    )

    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True),
        context=_context(adapter, buffers={SERVICE_A: (0, 15), SERVICE_B: (0, 0)}),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent is not None for step in planned.steps] == [True, True]
    assert [_time(step) for step in planned.steps] == ["12:00", "12:45"]


def test_no_joint_window_blocks_every_booking_in_the_group() -> None:
    adapter = _Adapter(
        {
            (SERVICE_A, DAY): _result(
                SERVICE_A,
                [_slot(SERVICE_A, DOCTOR_A, "2026-09-20T09:00:00+00:00", "2026-09-20T09:30:00+00:00")],
            ),
            (SERVICE_B, DAY): _result(SERVICE_B, []),
        }
    )

    planned = preflight_compound_visit_plan(
        _normalized(same_doctor=True),
        context=_context(adapter),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent for step in planned.steps] == [None, None]
    assert [step.disposition for step in planned.steps] == ["blocked", "blocked"]
    assert all(step.facts["compound_visit_no_joint_window"] is True for step in planned.steps)



def test_explicit_different_doctors_require_one_doctor_for_the_visit() -> None:
    adapter = _Adapter({})

    planned = preflight_compound_visit_plan(
        _normalized(),
        context=_context(adapter),
        timezone_name="Africa/Cairo",
    )

    assert [step.write_intent for step in planned.steps] == [None, None]
    assert [step.disposition for step in planned.steps] == ["clarify", "clarify"]
    assert [step.clarification_field for step in planned.steps] == ["doctor", "doctor"]
    assert all(step.facts["compound_visit_conflicting_doctors"] is True for step in planned.steps)


def test_grouped_missing_laser_device_requests_device_clarification() -> None:
    adapter = _joint_adapter()
    base = _normalized(same_doctor=True)
    standard = base.steps[0].model_copy(
        update={
            "facts": {
                **base.steps[0].facts,
                "service_requires_laser_device": False,
            }
        }
    )
    laser = base.steps[1].model_copy(
        update={
            "facts": {
                **base.steps[1].facts,
                "service_requires_laser_device": True,
            }
        }
    )

    planned = preflight_compound_visit_plan(
        base.model_copy(update={"steps": [standard, laser]}),
        context=_context(adapter),
        timezone_name="Africa/Cairo",
    )

    assert all(step.write_intent is None for step in planned.steps)
    assert planned.steps[0].disposition == "blocked"
    assert planned.steps[0].response_goal == "clarification"
    assert planned.steps[1].disposition == "clarify"
    assert planned.steps[1].clarification_field == "device"
    assert planned.steps[1].response_goal == "clarification"
    assert planned.steps[1].reads
    assert planned.steps[1].reads[0].kind == "availability"
    assert planned.steps[1].reads[0].parameters.get("device_key") is None
    assert planned.steps[1].facts["compound_visit_missing_device"] is True
    assert "compound_visit_no_joint_window" not in planned.steps[1].facts
    assert adapter.requests == []
