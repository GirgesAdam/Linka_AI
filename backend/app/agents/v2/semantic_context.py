from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.agents.v2.turn_contract import EntityReference, TiaTurnUnderstanding

ReferenceKind = Literal["service", "doctor", "device", "appointment", "package"]


@dataclass(frozen=True)
class SemanticReferenceTarget:
    kind: ReferenceKind
    canonical_id: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticContext:
    """Model-visible semantic catalog plus server-only reference resolution."""

    model_input: dict[str, object]
    reference_map: dict[str, SemanticReferenceTarget]
    server_metadata: dict[str, object] = field(default_factory=dict)

    def resolve(self, ref: str, *, expected_kind: ReferenceKind | None = None) -> str | None:
        target = self.reference_map.get(str(ref))
        if target is None:
            return None
        if expected_kind is not None and target.kind != expected_kind:
            return None
        return target.canonical_id


def _append_reference(
    reference_map: dict[str, SemanticReferenceTarget],
    *,
    prefix: str,
    index: int,
    kind: ReferenceKind,
    canonical_id: object,
    metadata: dict[str, object] | None = None,
) -> str | None:
    if canonical_id in (None, ""):
        return None
    ref = f"{prefix}{index}"
    reference_map[ref] = SemanticReferenceTarget(
        kind=kind,
        canonical_id=str(canonical_id),
        metadata=dict(metadata or {}),
    )
    return ref


def _clinic_operating_hours(clinic_catalog: dict[str, object]) -> list[dict[str, object]]:
    """Return one canonical single-location weekly schedule or fail closed.

    The V2 conversational product is single-location. Native clinic catalogs expose
    BranchWorkingHour rows as branches[].working_hours. If several branch rows are
    present, hours are exposed only when their schedules are identical; otherwise
    the semantic layer does not guess which location owns the customer's time.
    """
    raw_branches = clinic_catalog.get("branches")
    if not isinstance(raw_branches, list):
        return []

    schedules: list[list[dict[str, object]]] = []
    signatures: list[tuple[tuple[int, str, str], ...]] = []
    for branch in raw_branches:
        if not isinstance(branch, dict):
            continue
        raw_hours = branch.get("working_hours")
        if not isinstance(raw_hours, list):
            continue
        normalized: list[dict[str, object]] = []
        for row in raw_hours:
            if not isinstance(row, dict):
                continue
            weekday = row.get("weekday")
            start = str(row.get("start") or "")[:5]
            end = str(row.get("end") or "")[:5]
            if not isinstance(weekday, int) or not 0 <= weekday <= 6:
                continue
            if len(start) != 5 or len(end) != 5:
                continue
            normalized.append({"weekday": weekday, "start": start, "end": end})
        normalized.sort(key=lambda item: (int(item["weekday"]), str(item["start"]), str(item["end"])))
        signature = tuple(
            (int(item["weekday"]), str(item["start"]), str(item["end"]))
            for item in normalized
        )
        schedules.append(normalized)
        signatures.append(signature)

    if not schedules:
        return []
    if any(signature != signatures[0] for signature in signatures[1:]):
        return []
    return schedules[0]


def build_semantic_context(
    clinic_catalog: dict[str, object],
    *,
    active_task: dict[str, object] | None = None,
    pending_choice: dict[str, object] | None = None,
) -> SemanticContext:
    """Build compact global discovery indexes plus server-owned focus details.

    Database identities never enter model_input. Broad discovery keeps all service,
    doctor, and device identities available so a customer can change intent on any
    turn. Relationship/detail rows are normalized once and retained server-side for
    verified continuation focus.
    """

    _ = active_task, pending_choice
    reference_map: dict[str, SemanticReferenceTarget] = {}
    model_services: list[dict[str, object]] = []
    model_doctors: list[dict[str, object]] = []
    model_devices_by_ref: dict[str, dict[str, object]] = {}
    service_device_refs: dict[str, list[str]] = {}
    model_appointments: list[dict[str, object]] = []
    model_packages: list[dict[str, object]] = []
    focus_details: dict[str, dict[str, object]] = {}

    service_ref_by_id: dict[str, str] = {}
    doctor_ref_by_id: dict[str, str] = {}
    device_ref_by_key: dict[str, str] = {}

    services = clinic_catalog.get("services")
    if isinstance(services, list):
        for position, row in enumerate(services, start=1):
            if not isinstance(row, dict):
                continue
            canonical_id = row.get("id") or row.get("service_id")
            ref = _append_reference(
                reference_map,
                prefix="S",
                index=position,
                kind="service",
                canonical_id=canonical_id,
                metadata={
                    "requires_laser_device": row.get("requires_laser_device") is True,
                },
            )
            if ref is None:
                continue
            service_ref_by_id[str(canonical_id)] = ref
            model_services.append(
                {"ref": ref, "name": row.get("name") or row.get("service_name")}
            )
            detail: dict[str, object] = {"ref": ref}
            category = row.get("category")
            if category not in (None, ""):
                detail["category"] = category

            raw_devices = row.get("laser_devices")
            device_refs: list[str] = []
            if isinstance(raw_devices, list):
                for device in raw_devices:
                    if not isinstance(device, dict):
                        continue
                    device_key = device.get("device_key")
                    if device_key in (None, ""):
                        continue
                    key = str(device_key)
                    device_ref = device_ref_by_key.get(key)
                    if device_ref is None:
                        device_ref = _append_reference(
                            reference_map,
                            prefix="V",
                            index=len(device_ref_by_key) + 1,
                            kind="device",
                            canonical_id=key,
                            metadata={"device_name": device.get("device_name")},
                        )
                        if device_ref is None:
                            continue
                        device_ref_by_key[key] = device_ref
                        model_devices_by_ref[device_ref] = {
                            "ref": device_ref,
                            "name": device.get("device_name") or key,
                        }
                        focus_details[device_ref] = {
                            "ref": device_ref,
                            "service_refs": [],
                        }
                    device_refs.append(device_ref)
                    device_detail = focus_details.get(device_ref)
                    if isinstance(device_detail, dict):
                        service_refs = device_detail.setdefault("service_refs", [])
                        if isinstance(service_refs, list) and ref not in service_refs:
                            service_refs.append(ref)
            if device_refs:
                detail["device_refs"] = device_refs
                service_device_refs[ref] = list(device_refs)
            focus_details[ref] = detail

    doctors = clinic_catalog.get("doctors")
    if isinstance(doctors, list):
        for position, row in enumerate(doctors, start=1):
            if not isinstance(row, dict):
                continue
            canonical_id = row.get("id") or row.get("doctor_id")
            ref = _append_reference(
                reference_map,
                prefix="D",
                index=position,
                kind="doctor",
                canonical_id=canonical_id,
            )
            if ref is None:
                continue
            doctor_ref_by_id[str(canonical_id)] = ref
            item: dict[str, object] = {
                "ref": ref,
                "name": row.get("name") or row.get("doctor_name"),
            }
            model_doctors.append(item)

            detail: dict[str, object] = {"ref": ref}
            raw_service_ids = row.get("service_ids")
            service_refs = (
                [
                    service_ref_by_id[str(value)]
                    for value in raw_service_ids
                    if str(value) in service_ref_by_id
                ]
                if isinstance(raw_service_ids, list)
                else []
            )
            if service_refs:
                detail["service_refs"] = service_refs
                for service_ref in service_refs:
                    service_detail = focus_details.get(service_ref)
                    if isinstance(service_detail, dict):
                        doctor_refs = service_detail.setdefault("doctor_refs", [])
                        if isinstance(doctor_refs, list) and ref not in doctor_refs:
                            doctor_refs.append(ref)
            if row.get("specialization") not in (None, ""):
                detail["specialization"] = row.get("specialization")
            focus_details[ref] = detail

    appointments = clinic_catalog.get("appointments")
    if isinstance(appointments, list):
        appointment_position = 0
        for row in appointments:
            if not isinstance(row, dict):
                continue
            canonical_id = row.get("appointment_id") or row.get("id")
            appointment_position += 1
            ref = _append_reference(
                reference_map,
                prefix="A",
                index=appointment_position,
                kind="appointment",
                canonical_id=canonical_id,
            )
            if ref is None:
                continue
            item: dict[str, object] = {"ref": ref}
            service_id = row.get("service_id")
            doctor_id = row.get("doctor_id")
            if service_id is not None and str(service_id) in service_ref_by_id:
                item["service_ref"] = service_ref_by_id[str(service_id)]
            elif row.get("service_name"):
                item["service_name"] = row.get("service_name")
            if doctor_id is not None and str(doctor_id) in doctor_ref_by_id:
                item["doctor_ref"] = doctor_ref_by_id[str(doctor_id)]
            elif row.get("doctor_name"):
                item["doctor_name"] = row.get("doctor_name")
            for key in ("status", "start_local", "laser_device_name"):
                if row.get(key) not in (None, ""):
                    item[key] = row.get(key)
            device_key = row.get("laser_device_key")
            if device_key is not None and str(device_key) in device_ref_by_key:
                item["device_ref"] = device_ref_by_key[str(device_key)]
            model_appointments.append(item)
            focus_details[ref] = dict(item)

    packages = clinic_catalog.get("packages")
    if isinstance(packages, list):
        package_position = 0
        for row in packages:
            if not isinstance(row, dict):
                continue
            canonical_id = row.get("package_id") or row.get("id")
            package_position += 1
            ref = _append_reference(
                reference_map,
                prefix="P",
                index=package_position,
                kind="package",
                canonical_id=canonical_id,
            )
            if ref is None:
                continue
            item: dict[str, object] = {"ref": ref}
            for key in ("name", "remaining_sessions", "total_sessions", "status"):
                if row.get(key) not in (None, ""):
                    item[key] = row.get(key)
            service_id = row.get("service_id")
            if service_id is not None and str(service_id) in service_ref_by_id:
                item["service_ref"] = service_ref_by_id[str(service_id)]
            device_key = row.get("laser_device_key")
            if device_key is not None and str(device_key) in device_ref_by_key:
                item["device_ref"] = device_ref_by_key[str(device_key)]
            model_packages.append(item)
            focus_details[ref] = dict(item)

    model_input: dict[str, object] = {
        "services": model_services,
        "service_device_refs": service_device_refs,
        "devices": list(model_devices_by_ref.values()),
        "doctors": model_doctors,
        "appointments": model_appointments,
        "packages": model_packages,
        "focused_context": {},
        "active_task": {},
        "pending_choice": {},
        "recent_verified_action": {},
        "automation_context": {},
    }
    return SemanticContext(
        model_input=model_input,
        reference_map=reference_map,
        server_metadata={
            "clinic_operating_hours": _clinic_operating_hours(clinic_catalog),
            "focus_details": focus_details,
            "broad_appointments": tuple(model_appointments),
            "broad_packages": tuple(model_packages),
        },
    )

def _ground_entity(
    entity: EntityReference | None,
    *,
    expected_kind: ReferenceKind,
    context: SemanticContext,
) -> EntityReference | None:
    if entity is None:
        return None
    grounded_ref = (
        entity.ref
        if entity.ref is not None
        and context.resolve(entity.ref, expected_kind=expected_kind) is not None
        else None
    )
    grounded_candidates = [
        ref
        for ref in entity.candidate_refs
        if context.resolve(ref, expected_kind=expected_kind) is not None
    ]
    if grounded_ref is not None:
        grounded_candidates = []
    return entity.model_copy(
        update={
            "ref": grounded_ref,
            "candidate_refs": grounded_candidates,
        }
    )


def ground_turn_references(
    turn: TiaTurnUnderstanding,
    context: SemanticContext,
) -> TiaTurnUnderstanding:
    """Fail closed on invented/wrong-kind references without lexical recovery."""

    grounded_operations = []
    for operation in turn.operations:
        entities = operation.entities.model_copy(
            update={
                "service": _ground_entity(
                    operation.entities.service,
                    expected_kind="service",
                    context=context,
                ),
                "doctor": _ground_entity(
                    operation.entities.doctor,
                    expected_kind="doctor",
                    context=context,
                ),
                "device": _ground_entity(
                    operation.entities.device,
                    expected_kind="device",
                    context=context,
                ),
                "appointment": _ground_entity(
                    operation.entities.appointment,
                    expected_kind="appointment",
                    context=context,
                ),
                "package": _ground_entity(
                    operation.entities.package,
                    expected_kind="package",
                    context=context,
                ),
            }
        )
        selection = operation.selection
        if selection is not None and selection.kind == "ref":
            if selection.ref is None or context.resolve(selection.ref) is None:
                selection = None
        grounded_operations.append(
            operation.model_copy(
                update={
                    "entities": entities,
                    "selection": selection,
                }
            )
        )

    return turn.model_copy(update={"operations": grounded_operations})
