from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"Expected exactly one match in {path}: {old[:80]!r}; got {text.count(old)}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# 1) Provider-facing semantic contract can carry one grounded branch ref.
replace_once(
    "backend/app/agents/v2/turn_contract.py",
    "class TurnEntities(StrictContractModel):\n    service: EntityReference | None = None\n    doctor: EntityReference | None = None\n    device: EntityReference | None = None\n",
    "class TurnEntities(StrictContractModel):\n    service: EntityReference | None = None\n    doctor: EntityReference | None = None\n    branch: EntityReference | None = None\n    device: EntityReference | None = None\n",
)

# 2) Branches become opaque semantic refs and server-only branch-resolution metadata.
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    'ReferenceKind = Literal["service", "doctor", "device", "appointment", "package"]',
    'ReferenceKind = Literal["service", "doctor", "branch", "device", "appointment", "package"]',
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    "    model_services: list[dict[str, object]] = []\n    model_doctors: list[dict[str, object]] = []\n",
    "    model_services: list[dict[str, object]] = []\n    model_doctors: list[dict[str, object]] = []\n    model_branches: list[dict[str, object]] = []\n",
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    "    service_ref_by_id: dict[str, str] = {}\n    doctor_ref_by_id: dict[str, str] = {}\n",
    "    service_ref_by_id: dict[str, str] = {}\n    doctor_ref_by_id: dict[str, str] = {}\n    branch_ref_by_id: dict[str, str] = {}\n",
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    "    doctors = clinic_catalog.get(\"doctors\")\n",
    "    branches = clinic_catalog.get(\"branches\")\n    if isinstance(branches, list):\n        for position, row in enumerate(branches, start=1):\n            if not isinstance(row, dict):\n                continue\n            canonical_id = row.get(\"id\") or row.get(\"branch_id\")\n            ref = _append_reference(\n                reference_map,\n                prefix=\"B\",\n                index=position,\n                kind=\"branch\",\n                canonical_id=canonical_id,\n            )\n            if ref is None:\n                continue\n            branch_ref_by_id[str(canonical_id)] = ref\n            model_branches.append(\n                {\n                    \"ref\": ref,\n                    \"name\": row.get(\"name\") or row.get(\"branch_name\"),\n                    \"city\": row.get(\"city\"),\n                }\n            )\n            focus_details[ref] = {\"ref\": ref}\n\n    doctors = clinic_catalog.get(\"doctors\")\n",
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    "            if row.get(\"specialization\") not in (None, \"\"):\n                detail[\"specialization\"] = row.get(\"specialization\")\n            focus_details[ref] = detail\n",
    "            raw_branch_ids = row.get(\"branch_ids\")\n            branch_refs = (\n                [\n                    branch_ref_by_id[str(value)]\n                    for value in raw_branch_ids\n                    if str(value) in branch_ref_by_id\n                ]\n                if isinstance(raw_branch_ids, list)\n                else []\n            )\n            if branch_refs:\n                detail[\"branch_refs\"] = branch_refs\n            if row.get(\"specialization\") not in (None, \"\"):\n                detail[\"specialization\"] = row.get(\"specialization\")\n            focus_details[ref] = detail\n",
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    '        "doctors": model_doctors,\n        "appointments": model_appointments,\n',
    '        "doctors": model_doctors,\n        "branches": model_branches,\n        "appointments": model_appointments,\n',
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    '            "clinic_operating_hours": _clinic_operating_hours(clinic_catalog),\n            "focus_details": focus_details,\n',
    '            "clinic_operating_hours": _clinic_operating_hours(clinic_catalog),\n            "branch_resolution_catalog": {\n                "primary_branch_id": clinic_catalog.get("primary_branch_id"),\n                "branches": [dict(row) for row in branches if isinstance(row, dict)]\n                if isinstance(branches, list)\n                else [],\n                "doctors": [dict(row) for row in doctors if isinstance(row, dict)]\n                if isinstance(doctors, list)\n                else [],\n            },\n            "focus_details": focus_details,\n',
)
replace_once(
    "backend/app/agents/v2/semantic_context.py",
    '                "doctor": _ground_entity(\n                    operation.entities.doctor,\n                    expected_kind="doctor",\n                    context=context,\n                ),\n                "device": _ground_entity(',
    '                "doctor": _ground_entity(\n                    operation.entities.doctor,\n                    expected_kind="doctor",\n                    context=context,\n                ),\n                "branch": _ground_entity(\n                    operation.entities.branch,\n                    expected_kind="branch",\n                    context=context,\n                ),\n                "device": _ground_entity(',
)

# 3) Durable task state owns the grounded branch just like other scheduling constraints.
replace_once(
    "backend/app/services/agent_v2/state.py",
    '    "doctor",\n    "device",\n',
    '    "doctor",\n    "branch",\n    "device",\n',
)
replace_once(
    "backend/app/services/agent_v2/state.py",
    "    service_id: str | None = None\n    doctor_id: str | None = None\n    device_key: str | None = None\n",
    "    service_id: str | None = None\n    doctor_id: str | None = None\n    branch_id: str | None = None\n    device_key: str | None = None\n",
)

# 4) Safe semantic state projection exposes only branch refs, never canonical IDs.
replace_once(
    "backend/app/agents/v2/semantic_state_view.py",
    '        ("doctor_id", "doctor", "doctor_ref"),\n        ("device_key", "device", "device_ref"),\n',
    '        ("doctor_id", "doctor", "doctor_ref"),\n        ("branch_id", "branch", "branch_ref"),\n        ("device_key", "device", "device_ref"),\n',
)
replace_once(
    "backend/app/agents/v2/semantic_state_view.py",
    '    "doctor_id": "doctor",\n    "device_key": "device",\n',
    '    "doctor_id": "doctor",\n    "branch_id": "branch",\n    "device_key": "device",\n',
)

# 5) Small shared backend authority helper: explicit > compatible primary/single > clarify.
helper_path = ROOT / "backend/app/services/agent_v2/branch_task_resolution.py"
helper_path.write_text(
    '''from __future__ import annotations\n\nfrom app.agents.v2.semantic_context import SemanticContext\nfrom app.services.agent_v2.branch_resolution import (\n    BranchResolutionError,\n    resolve_single_location_branch_id,\n)\n\n\ndef _catalog(context: SemanticContext) -> dict[str, object]:\n    raw = context.server_metadata.get("branch_resolution_catalog")\n    return dict(raw) if isinstance(raw, dict) else {"branches": []}\n\n\ndef _compatible_catalog(\n    catalog: dict[str, object],\n    *,\n    doctor_id: str | None,\n) -> dict[str, object]:\n    if not doctor_id:\n        return catalog\n    doctors = catalog.get("doctors")\n    allowed: set[str] | None = None\n    if isinstance(doctors, list):\n        for row in doctors:\n            if not isinstance(row, dict) or str(row.get("id") or "") != doctor_id:\n                continue\n            raw = row.get("branch_ids")\n            allowed = {str(value) for value in raw if value not in (None, "")} if isinstance(raw, list) else set()\n            break\n    if allowed is None:\n        return catalog\n    branches = catalog.get("branches")\n    filtered = [\n        dict(row)\n        for row in branches\n        if isinstance(row, dict) and str(row.get("id") or row.get("branch_id") or "") in allowed\n    ] if isinstance(branches, list) else []\n    primary = catalog.get("primary_branch_id")\n    if primary not in (None, "") and str(primary) not in allowed:\n        primary = None\n    return {**catalog, "primary_branch_id": primary, "branches": filtered}\n\n\ndef _refs_for_branch_ids(context: SemanticContext, branch_ids: set[str]) -> list[str]:\n    return [\n        ref\n        for ref, target in context.reference_map.items()\n        if target.kind == "branch" and target.canonical_id in branch_ids\n    ]\n\n\ndef resolve_scheduling_branch_parameters(\n    params: dict[str, object],\n    *,\n    context: SemanticContext,\n) -> tuple[dict[str, object], list[str], str | None]:\n    """Resolve branch only from canonical backend facts; never from customer text.\n\n    Returns (parameters, candidate_refs, reason). A non-null reason means the caller\n    must clarify and must not perform an availability read/write.\n    """\n    explicit = str(params.get("branch_id") or "").strip()\n    catalog = _compatible_catalog(\n        _catalog(context),\n        doctor_id=str(params.get("doctor_id") or "").strip() or None,\n    )\n    branches = catalog.get("branches")\n    branch_ids = {\n        str(row.get("id") or row.get("branch_id"))\n        for row in branches\n        if isinstance(row, dict) and (row.get("id") or row.get("branch_id")) not in (None, "")\n    } if isinstance(branches, list) else set()\n\n    if explicit:\n        if branch_ids and explicit not in branch_ids:\n            return params, _refs_for_branch_ids(context, branch_ids), "incompatible_branch"\n        return params, [], None\n\n    try:\n        resolved = resolve_single_location_branch_id(explicit_ref=None, catalog=catalog)\n    except BranchResolutionError:\n        refs = _refs_for_branch_ids(context, branch_ids)\n        return params, refs, "branch_choice" if refs else "no_compatible_branch"\n    return {**params, "branch_id": resolved}, [], None\n''',
    encoding="utf-8",
)

# 6) Planner understands branch refs and blocks ambiguous reads before the read executor.
replace_once(
    "backend/app/services/agent_v2/planner.py",
    "from app.services.agent_v2.compound_turn_policy import (\n",
    "from app.services.agent_v2.branch_task_resolution import resolve_scheduling_branch_parameters\nfrom app.services.agent_v2.compound_turn_policy import (\n",
)
replace_once(
    "backend/app/services/agent_v2/planner.py",
    '    "doctor",\n    "device",\n',
    '    "doctor",\n    "branch",\n    "device",\n',
)
replace_once(
    "backend/app/services/agent_v2/planner.py",
    '        ("doctor", "doctor", "doctor_id", "doctor_ids"),\n        ("device", "device", "device_key", "device_keys"),\n',
    '        ("doctor", "doctor", "doctor_id", "doctor_ids"),\n        ("branch", "branch", "branch_id", "branch_ids"),\n        ("device", "device", "device_key", "device_keys"),\n',
)
replace_once(
    "backend/app/services/agent_v2/planner.py",
    "    if operation.type == \"availability\":\n        reference_step = _plan_verified_availability_reference(\n",
    "    if operation.type == \"availability\":\n        reference_step = _plan_verified_availability_reference(\n",
)
replace_once(
    "backend/app/services/agent_v2/planner.py",
    "        if \"service_id\" not in params:\n            return _clarify(index=index, operation=operation, field=\"service\")\n        return PlanStep(\n            operation_index=index,\n            operation_type=operation.type,\n            disposition=\"read\",\n            reads=[ReadRequest(kind=\"availability\", parameters=params)],\n",
    "        if \"service_id\" not in params:\n            return _clarify(index=index, operation=operation, field=\"service\")\n        params, branch_refs, branch_reason = resolve_scheduling_branch_parameters(\n            params, context=context.semantic_context\n        )\n        if branch_reason is not None:\n            return _clarify(\n                index=index,\n                operation=operation,\n                field=\"branch\",\n                facts={\n                    **params,\n                    \"_verified_candidate_refs\": branch_refs,\n                    \"branch_resolution_reason\": branch_reason,\n                },\n            )\n        return PlanStep(\n            operation_index=index,\n            operation_type=operation.type,\n            disposition=\"read\",\n            reads=[ReadRequest(kind=\"availability\", parameters=params)],\n",
)
replace_once(
    "backend/app/services/agent_v2/planner.py",
    "        if not compound_book:\n            commercial_path = (\n",
    "        if \"date\" in params:\n            params, branch_refs, branch_reason = resolve_scheduling_branch_parameters(\n                params, context=context.semantic_context\n            )\n            if branch_reason is not None:\n                return _clarify(\n                    index=index,\n                    operation=operation,\n                    field=\"branch\",\n                    facts={\n                        **params,\n                        \"_verified_candidate_refs\": branch_refs,\n                        \"branch_resolution_reason\": branch_reason,\n                    },\n                )\n        if not compound_book:\n            commercial_path = (\n",
)

# 7) Active booking continuation resolves/persists branch before any availability read.
replace_once(
    "backend/app/services/agent_v2/active_task_progress.py",
    "from app.agents.v2.turn_contract import TurnOperation\n",
    "from app.agents.v2.turn_contract import TurnOperation\nfrom app.services.agent_v2.branch_task_resolution import resolve_scheduling_branch_parameters\n",
)
replace_once(
    "backend/app/services/agent_v2/active_task_progress.py",
    '        ("doctor", "doctor", "doctor_id"),\n        ("device", "device", "device_key"),\n',
    '        ("doctor", "doctor", "doctor_id"),\n        ("branch", "branch", "branch_id"),\n        ("device", "device", "device_key"),\n',
)
replace_once(
    "backend/app/services/agent_v2/active_task_progress.py",
    '        ("doctor", "doctor_id"),\n        ("device", "device_key"),\n',
    '        ("doctor", "doctor_id"),\n        ("branch", "branch_id"),\n        ("device", "device_key"),\n',
)
replace_once(
    "backend/app/services/agent_v2/active_task_progress.py",
    "    commercial_key = _commercial_basis_key(state)\n",
    "    if state.constraints.date is not None:\n        params, branch_refs, branch_reason = resolve_scheduling_branch_parameters(\n            params, context=context\n        )\n        if branch_reason is not None:\n            return PlanStep(\n                operation_index=operation_index,\n                operation_type=\"continue_active\",\n                disposition=\"clarify\",\n                state_action=\"update_active\",\n                response_goal=\"clarification\",\n                clarification_field=\"branch\",\n                facts={\n                    **params,\n                    \"_verified_candidate_refs\": branch_refs,\n                    \"branch_resolution_reason\": branch_reason,\n                },\n            )\n    commercial_key = _commercial_basis_key(state)\n",
)

# 8) State transitions apply branch updates and invalidate availability only.
replace_once(
    "backend/app/services/agent_v2/state_rules.py",
    "def apply_booking_device_change(\n",
    "def apply_booking_branch_change(\n    state: BookingTaskState,\n    *,\n    branch_id: str | None,\n) -> BookingTaskState:\n    if state.constraints.branch_id == branch_id:\n        return state\n    constraints = state.constraints.model_copy(update={\"branch_id\": branch_id})\n    derived = _booking_derived(state, clear_availability=True)\n    return _next_booking_state(state, constraints=constraints, derived=derived)\n\n\ndef apply_booking_device_change(\n",
)
replace_once(
    "backend/app/services/agent_v2/state_executor.py",
    "    apply_booking_date_change,\n    apply_booking_device_change,\n",
    "    apply_booking_branch_change,\n    apply_booking_date_change,\n    apply_booking_device_change,\n",
)
replace_once(
    "backend/app/services/agent_v2/state_executor.py",
    "        doctor_id=str(facts[\"doctor_id\"]) if facts.get(\"doctor_id\") else None,\n        device_key=str(facts[\"device_key\"]) if facts.get(\"device_key\") else None,\n",
    "        doctor_id=str(facts[\"doctor_id\"]) if facts.get(\"doctor_id\") else None,\n        branch_id=str(facts[\"branch_id\"]) if facts.get(\"branch_id\") else None,\n        device_key=str(facts[\"device_key\"]) if facts.get(\"device_key\") else None,\n",
)
replace_once(
    "backend/app/services/agent_v2/state_executor.py",
    "    if \"device_key\" in facts:\n        updated = apply_booking_device_change(\n",
    "    if \"branch_id\" in facts:\n        updated = apply_booking_branch_change(\n            updated,\n            branch_id=str(facts[\"branch_id\"]) if facts.get(\"branch_id\") else None,\n        )\n    if \"device_key\" in facts:\n        updated = apply_booking_device_change(\n",
)

# 9) Grouped components preserve branch across expansion/restore/correction.
replace_once(
    "backend/app/services/agent_v2/grouped_booking_continuity.py",
    "            \"doctor_id\": str(facts[\"doctor_id\"]) if facts.get(\"doctor_id\") else None,\n            \"device_key\": str(facts[\"device_key\"]) if facts.get(\"device_key\") else None,\n",
    "            \"doctor_id\": str(facts[\"doctor_id\"]) if facts.get(\"doctor_id\") else None,\n            \"branch_id\": str(facts[\"branch_id\"]) if facts.get(\"branch_id\") else None,\n            \"device_key\": str(facts[\"device_key\"]) if facts.get(\"device_key\") else None,\n",
)
replace_once(
    "backend/app/services/agent_v2/grouped_booking_continuity.py",
    "    device_ref = _canonical_ref(\n        context,\n        kind=\"device\",\n",
    "    branch_ref = _canonical_ref(\n        context,\n        kind=\"branch\",\n        canonical_id=component.branch_id,\n    )\n    if component.branch_id is not None and branch_ref is None:\n        return None\n    device_ref = _canonical_ref(\n        context,\n        kind=\"device\",\n",
)
replace_once(
    "backend/app/services/agent_v2/grouped_booking_continuity.py",
    '            "doctor": _entity_ref(doctor_ref),\n            "device": _entity_ref(device_ref),\n',
    '            "doctor": _entity_ref(doctor_ref),\n            "branch": _entity_ref(branch_ref),\n            "device": _entity_ref(device_ref),\n',
)
replace_once(
    "backend/app/services/agent_v2/grouped_booking_continuity.py",
    '    for key in ("doctor_id", "date", "time"):\n',
    '    for key in ("doctor_id", "branch_id", "date", "time"):\n',
)

# 10) Customer-facing branch clarification can show grounded branch names.
replace_once(
    "backend/app/services/agent_v2/outcome_builder.py",
    '    for collection in ("services", "doctors", "appointments", "packages"):\n',
    '    for collection in ("services", "doctors", "branches", "appointments", "packages"):\n',
)
replace_once(
    "backend/app/services/agent_v2/outcome_builder.py",
    '    if step.clarification_field not in {"service", "doctor", "device", "appointment", "package"}:\n',
    '    if step.clarification_field not in {"service", "doctor", "branch", "device", "appointment", "package"}:\n',
)
replace_once(
    "backend/app/services/agent_v2/outcome_builder.py",
    "    entity = getattr(operation.entities, step.clarification_field)\n    if entity is None:\n        return []\n    choices: list[OutcomeChoice] = []\n    verified_refs = step.facts.get(\"_verified_candidate_refs\")\n    candidate_refs = verified_refs if isinstance(verified_refs, list) else entity.candidate_refs\n",
    "    entity = getattr(operation.entities, step.clarification_field)\n    choices: list[OutcomeChoice] = []\n    verified_refs = step.facts.get(\"_verified_candidate_refs\")\n    if entity is None and not isinstance(verified_refs, list):\n        return []\n    candidate_refs = (\n        verified_refs\n        if isinstance(verified_refs, list)\n        else entity.candidate_refs\n        if entity is not None\n        else []\n    )\n",
)

# 11) Focused permanent regression coverage.
test_path = ROOT / "backend/tests/test_v2_multi_service_branch_continuation.py"
test_path.write_text(
    '''from __future__ import annotations\n\nfrom datetime import UTC, datetime\n\nfrom app.agents.v2.semantic_context import build_semantic_context, ground_turn_references\nfrom app.agents.v2.turn_contract import (\n    DateConstraint,\n    EntityReference,\n    TiaTurnUnderstanding,\n    TurnEntities,\n    TurnOperation,\n)\nfrom app.services.agent_v2.active_task_progress import plan_active_task_progress\nfrom app.services.agent_v2.branch_task_resolution import resolve_scheduling_branch_parameters\nfrom app.services.agent_v2.grouped_booking_continuity import (\n    restore_grouped_booking_understanding,\n)\nfrom app.services.agent_v2.state import (\n    BookingTaskState,\n    CustomerConstraints,\n    GroupedBookingState,\n    WriteAuthorization,\n)\nfrom app.services.agent_v2.turn_normalization import expand_multi_service_operations\n\n\ndef _catalog(*, branches: int = 1) -> dict[str, object]:\n    branch_rows = [\n        {\"id\": f\"branch-{index}\", \"name\": f\"Branch {index}\", \"city\": \"Cairo\"}\n        for index in range(1, branches + 1)\n    ]\n    return {\n        \"services\": [\n            {\"id\": \"s1\", \"name\": \"إبط\", \"requires_laser_device\": False},\n            {\"id\": \"s2\", \"name\": \"بكيني\", \"requires_laser_device\": False},\n            {\"id\": \"s3\", \"name\": \"ذراعين كاملين\", \"requires_laser_device\": False},\n        ],\n        \"branches\": branch_rows,\n        \"doctors\": [\n            {\n                \"id\": \"d1\",\n                \"name\": \"Doctor\",\n                \"service_ids\": [\"s1\", \"s2\", \"s3\"],\n                \"branch_ids\": [row[\"id\"] for row in branch_rows],\n            }\n        ],\n        \"appointments\": [],\n        \"packages\": [],\n    }\n\n\ndef _book(service_ref: str, *, branch_ref: str | None = None, date: bool = False) -> TurnOperation:\n    return TurnOperation(\n        type=\"book\",\n        entities=TurnEntities(\n            service=EntityReference(text=None, ref=service_ref),\n            branch=EntityReference(text=\"branch\", ref=branch_ref) if branch_ref else None,\n            date=DateConstraint(mode=\"exact\", start_date=\"2026-10-13\") if date else None,\n        ),\n        execution_intent=\"execute\",\n    )\n\n\ndef _authorized() -> WriteAuthorization:\n    return WriteAuthorization(\n        operation=\"booking\",\n        authorized=True,\n        source_turn_id=\"turn-1\",\n        granted_at=datetime(2026, 10, 11, tzinfo=UTC),\n    )\n\n\ndef test_semantic_context_exposes_and_grounds_branch_refs() -> None:\n    context = build_semantic_context(_catalog(branches=2))\n    assert [row[\"name\"] for row in context.model_input[\"branches\"]] == [\"Branch 1\", \"Branch 2\"]\n    turn = TiaTurnUnderstanding(\n        operations=[_book(\"S1\", branch_ref=\"B2\")],\n        safety_signals=[],\n    )\n    grounded = ground_turn_references(turn, context)\n    assert grounded.operations[0].entities.branch is not None\n    assert grounded.operations[0].entities.branch.ref == \"B2\"\n    assert context.resolve(\"B2\", expected_kind=\"branch\") == \"branch-2\"\n\n\ndef test_multi_service_expansion_preserves_explicit_branch() -> None:\n    context = build_semantic_context(_catalog(branches=2))\n    operation = TurnOperation(\n        type=\"book\",\n        entities=TurnEntities(\n            service=EntityReference(\n                text=\"إبط وبكيني وذراعين\",\n                ref=None,\n                candidate_refs=[\"S1\", \"S2\", \"S3\"],\n                candidate_mode=\"set\",\n            ),\n            branch=EntityReference(text=\"Branch 2\", ref=\"B2\"),\n        ),\n        execution_intent=\"execute\",\n    )\n    expanded, groups = expand_multi_service_operations(\n        TiaTurnUnderstanding(operations=[operation], safety_signals=[]),\n        semantic_context=context,\n    )\n    assert len(expanded.operations) == 3\n    assert len(groups) == 3\n    assert {item.entities.branch.ref for item in expanded.operations if item.entities.branch} == {\"B2\"}\n\n\ndef test_single_branch_resolves_deterministically_before_availability() -> None:\n    context = build_semantic_context(_catalog(branches=1))\n    params, refs, reason = resolve_scheduling_branch_parameters(\n        {\"service_id\": \"s1\"}, context=context\n    )\n    assert reason is None\n    assert refs == []\n    assert params[\"branch_id\"] == \"branch-1\"\n\n\ndef test_multiple_branches_require_grounded_choice() -> None:\n    context = build_semantic_context(_catalog(branches=2))\n    params, refs, reason = resolve_scheduling_branch_parameters(\n        {\"service_id\": \"s1\"}, context=context\n    )\n    assert params.get(\"branch_id\") is None\n    assert reason == \"branch_choice\"\n    assert refs == [\"B1\", \"B2\"]\n\n\ndef test_explicit_branch_is_preserved_by_backend_resolution() -> None:\n    context = build_semantic_context(_catalog(branches=2))\n    params, refs, reason = resolve_scheduling_branch_parameters(\n        {\"service_id\": \"s1\", \"branch_id\": \"branch-2\"}, context=context\n    )\n    assert reason is None\n    assert refs == []\n    assert params[\"branch_id\"] == \"branch-2\"\n\n\ndef test_grouped_date_continuation_single_branch_reaches_availability() -> None:\n    context = build_semantic_context(_catalog(branches=1))\n    date = DateConstraint(mode=\"exact\", start_date=\"2026-10-13\")\n    components = [\n        CustomerConstraints(service_id=service_id, date=date)\n        for service_id in (\"s1\", \"s2\", \"s3\")\n    ]\n    task = BookingTaskState(\n        write_authorization=_authorized(),\n        constraints=components[0],\n        grouped=GroupedBookingState(components=components),\n    )\n    step = plan_active_task_progress(task, operation_index=0, context=context)\n    assert step.disposition == \"read\"\n    assert step.clarification_field is None\n    assert step.reads[-1].kind == \"availability\"\n    assert step.reads[-1].parameters[\"branch_id\"] == \"branch-1\"\n\n\ndef test_grouped_date_continuation_multi_branch_clarifies_before_read() -> None:\n    context = build_semantic_context(_catalog(branches=3))\n    date = DateConstraint(mode=\"exact\", start_date=\"2026-10-13\")\n    components = [\n        CustomerConstraints(service_id=service_id, date=date)\n        for service_id in (\"s1\", \"s2\", \"s3\")\n    ]\n    task = BookingTaskState(\n        write_authorization=_authorized(),\n        constraints=components[0],\n        grouped=GroupedBookingState(components=components),\n    )\n    step = plan_active_task_progress(task, operation_index=0, context=context)\n    assert step.disposition == \"clarify\"\n    assert step.clarification_field == \"branch\"\n    assert step.reads == []\n    assert step.write_intent is None\n    assert step.facts[\"_verified_candidate_refs\"] == [\"B1\", \"B2\", \"B3\"]\n\n\ndef test_grouped_restore_applies_continuation_branch_to_every_component() -> None:\n    context = build_semantic_context(_catalog(branches=2))\n    components = [CustomerConstraints(service_id=value) for value in (\"s1\", \"s2\", \"s3\")]\n    task = BookingTaskState(\n        write_authorization=_authorized(),\n        constraints=components[0],\n        grouped=GroupedBookingState(components=components),\n    )\n    continuation = TurnOperation(\n        type=\"continue_active\",\n        entities=TurnEntities(\n            branch=EntityReference(text=\"Branch 2\", ref=\"B2\"),\n            date=DateConstraint(mode=\"exact\", start_date=\"2026-10-13\"),\n        ),\n        execution_intent=\"execute\",\n    )\n    restored, groups = restore_grouped_booking_understanding(\n        TiaTurnUnderstanding(operations=[continuation], safety_signals=[]),\n        active_task=task,\n        context=context,\n    )\n    assert len(restored.operations) == 3\n    assert len(groups) == 3\n    for operation in restored.operations:\n        assert operation.entities.branch is not None\n        assert operation.entities.branch.ref == \"B2\"\n        assert operation.entities.date is not None\n        assert operation.entities.date.start_date == \"2026-10-13\"\n''',
    encoding="utf-8",
)

print("Applied multi-service branch continuation patch")
