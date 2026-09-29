from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable
from copy import deepcopy
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.clinic.registry import get_clinic_adapter
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)


def _filter_bookable_doctor_rows(
    rows: Iterable[tuple[Any, Any]],
    *,
    service_ids_by_doctor: dict[Any, Iterable[Any]],
    branch_ids_by_doctor: dict[Any, Iterable[Any]],
    scheduled_branch_ids_by_doctor: dict[Any, Iterable[Any]],
) -> list[tuple[Any, Any]]:
    """Keep doctors that have a complete bookable service/branch/schedule graph."""
    result: list[tuple[Any, Any]] = []

    for doctor, staff in rows:
        doctor_id = doctor.id
        service_ids = set(service_ids_by_doctor.get(doctor_id, ()))
        branch_ids = set(branch_ids_by_doctor.get(doctor_id, ()))
        scheduled_branch_ids = set(
            scheduled_branch_ids_by_doctor.get(doctor_id, ())
        )

        if not service_ids:
            continue
        if not branch_ids:
            continue
        if not branch_ids.intersection(scheduled_branch_ids):
            continue

        result.append((doctor, staff))

    return result


_CATALOG_CACHE_MAX_ENTRIES = 128
_catalog_cache_lock = Lock()
_catalog_cache: OrderedDict[UUID, tuple[object, dict[str, Any]]] = OrderedDict()


def _catalog_cache_get(
    workspace_id: UUID,
    signature: object,
) -> dict[str, Any] | None:
    with _catalog_cache_lock:
        cached = _catalog_cache.get(workspace_id)
        if cached is None or cached[0] != signature:
            return None
        _catalog_cache.move_to_end(workspace_id)
        return deepcopy(cached[1])


def _catalog_cache_put(
    workspace_id: UUID,
    signature: object,
    catalog: dict[str, Any],
) -> None:
    with _catalog_cache_lock:
        _catalog_cache[workspace_id] = (signature, deepcopy(catalog))
        _catalog_cache.move_to_end(workspace_id)
        while len(_catalog_cache) > _CATALOG_CACHE_MAX_ENTRIES:
            _catalog_cache.popitem(last=False)


def _annotate_catalog_relationships(catalog: dict[str, Any]) -> dict[str, Any]:
    """Expose only canonical operational facts and entity relationships for grounding.

    Adapters already provide doctor -> service/branch IDs. Mirroring the service
    relationship makes cross-entity consistency explicit to the semantic model.
    Free-form service descriptions are deliberately removed here so every agent
    path uses the saved clinic knowledge text as the sole explanatory prose source.
    """
    service_rows = catalog.get("services")
    doctor_rows = catalog.get("doctors")
    if not isinstance(service_rows, list) or not isinstance(doctor_rows, list):
        return catalog

    known_service_ids = {
        str(row.get("id"))
        for row in service_rows
        if isinstance(row, dict) and row.get("id")
    }
    doctor_ids_by_service: dict[str, set[str]] = {
        service_id: set() for service_id in known_service_ids
    }

    for doctor in doctor_rows:
        if not isinstance(doctor, dict) or not doctor.get("id"):
            continue
        doctor_id = str(doctor["id"])
        raw_service_ids = doctor.get("service_ids")
        if not isinstance(raw_service_ids, list):
            continue
        for value in raw_service_ids:
            service_id = str(value)
            if service_id in doctor_ids_by_service:
                doctor_ids_by_service[service_id].add(doctor_id)

    for service in service_rows:
        if not isinstance(service, dict):
            continue
        service.pop("description", None)
        if not service.get("id"):
            continue
        service_id = str(service["id"])
        service["doctor_ids"] = sorted(doctor_ids_by_service.get(service_id, set()))

    return catalog


def build_clinic_catalog(db: Session, workspace: Workspace) -> dict[str, Any]:
    """Build/reuse the canonical clinic catalog through the workspace adapter.

    The semantic layer no longer knows how services, branches, doctors, or
    schedules are stored. Linka's native adapter currently reads PostgreSQL; a
    future clinic adapter can provide the same canonical catalog from an API or
    another source system without changing the LLM interpreter.
    """
    adapter = get_clinic_adapter(db=db, workspace=workspace)

    revision_started = perf_counter()
    revision = adapter.catalog_revision()
    revision_ms = int((perf_counter() - revision_started) * 1000)
    cache_signature = (adapter.cache_namespace, revision) if revision is not None else None

    if cache_signature is not None:
        cached = _catalog_cache_get(workspace.id, cache_signature)
        if cached is not None:
            logger.info(
                "Linka clinic catalog workspace_id=%s adapter=%s cache_hit=true revision_ms=%s",
                workspace.id,
                adapter.cache_namespace,
                revision_ms,
            )
            return cached

    build_started = perf_counter()
    catalog = _annotate_catalog_relationships(adapter.build_catalog())
    build_ms = int((perf_counter() - build_started) * 1000)

    if cache_signature is not None:
        _catalog_cache_put(workspace.id, cache_signature, catalog)

    logger.info(
        "Linka clinic catalog workspace_id=%s adapter=%s cache_hit=false revision_ms=%s build_ms=%s",
        workspace.id,
        adapter.cache_namespace,
        revision_ms,
        build_ms,
    )
    return deepcopy(catalog)


def _catalog_ids(catalog: dict[str, Any], collection: str) -> set[str]:
    values = catalog.get(collection)
    if not isinstance(values, list):
        return set()
    return {
        str(item.get("id"))
        for item in values
        if isinstance(item, dict) and item.get("id")
    }


def _catalog_row_by_id(
    catalog: dict[str, Any],
    collection: str,
    canonical_id: str | None,
) -> dict[str, Any] | None:
    if not canonical_id:
        return None
    rows = catalog.get(collection)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == str(canonical_id):
            return row
    return None


def _valid_id(value: str | None, allowed: set[str]) -> str | None:
    if not value:
        return None
    candidate = str(value)
    return candidate if candidate in allowed else None


def _valid_ids(values: Iterable[str] | None, allowed: set[str]) -> list[str]:
    if values is None:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = str(value)
        if candidate in allowed and candidate not in seen:
            result.append(candidate)
            seen.add(candidate)
    return result


def _filter_candidates_for_selected_doctor(
    *,
    selected_id: str | None,
    candidate_ids: list[str],
    allowed_ids: set[str],
    allow_promotion: bool,
) -> tuple[str | None, list[str]]:
    """Apply one canonical doctor relationship without guessing from customer text.

    A single compatible candidate may become selected only when the model did not
    provide an explicit selected ID. If an explicit ID was hallucinated or was
    incompatible, validation rejects it but does not silently replace that model
    decision in the same pass.
    """
    if not allowed_ids:
        return selected_id, candidate_ids

    compatible_candidates = [
        candidate_id for candidate_id in candidate_ids if candidate_id in allowed_ids
    ]
    if selected_id is not None and selected_id not in allowed_ids:
        selected_id = None

    if selected_id is not None:
        return selected_id, []
    if allow_promotion and len(compatible_candidates) == 1:
        return compatible_candidates[0], []
    return None, compatible_candidates


def validate_grounded_entity_ids(entity_hints: Any, catalog: dict[str, Any]):
    """Validate canonical IDs and cross-entity compatibility.

    The model may only return IDs from the supplied catalog. Canonical catalog
    relationships are also authoritative for resource requirements: once a
    selected service is known not to use a laser device, irrelevant model or
    conversation device state is discarded without inspecting customer wording.
    """
    service_ids = _catalog_ids(catalog, "services")
    branch_ids = _catalog_ids(catalog, "branches")
    doctor_ids = _catalog_ids(catalog, "doctors")
    appointment_ids = _catalog_ids(catalog, "appointments")

    raw_service_id = getattr(entity_hints, "service_id", None)
    raw_branch_id = getattr(entity_hints, "branch_id", None)

    service_id = _valid_id(raw_service_id, service_ids)
    service_candidate_ids = _valid_ids(
        getattr(entity_hints, "service_candidate_ids", []), service_ids
    )
    branch_id = _valid_id(raw_branch_id, branch_ids)
    branch_candidate_ids = _valid_ids(
        getattr(entity_hints, "branch_candidate_ids", []), branch_ids
    )
    doctor_id = _valid_id(getattr(entity_hints, "doctor_id", None), doctor_ids)
    doctor_candidate_ids = _valid_ids(
        getattr(entity_hints, "doctor_candidate_ids", []), doctor_ids
    )
    appointment_id = _valid_id(
        getattr(entity_hints, "appointment_id", None), appointment_ids
    )

    doctor_row = _catalog_row_by_id(catalog, "doctors", doctor_id)
    if doctor_row is not None:
        doctor_service_ids = {
            str(value) for value in (doctor_row.get("service_ids") or []) if value
        }
        service_id, service_candidate_ids = _filter_candidates_for_selected_doctor(
            selected_id=service_id,
            candidate_ids=service_candidate_ids,
            allowed_ids=doctor_service_ids,
            allow_promotion=not bool(raw_service_id),
        )

        doctor_branch_ids = {
            str(value) for value in (doctor_row.get("branch_ids") or []) if value
        }
        branch_id, branch_candidate_ids = _filter_candidates_for_selected_doctor(
            selected_id=branch_id,
            candidate_ids=branch_candidate_ids,
            allowed_ids=doctor_branch_ids,
            allow_promotion=not bool(raw_branch_id),
        )

    laser_device_key = getattr(entity_hints, "laser_device_key", None)
    selected_service = _catalog_row_by_id(catalog, "services", service_id)
    if (
        selected_service is not None
        and "requires_laser_device" in selected_service
        and not bool(selected_service.get("requires_laser_device"))
    ):
        laser_device_key = None

    return entity_hints.model_copy(
        update={
            "service_id": service_id,
            "service_candidate_ids": service_candidate_ids,
            "branch_id": branch_id,
            "branch_candidate_ids": branch_candidate_ids,
            "doctor_id": doctor_id,
            "doctor_candidate_ids": doctor_candidate_ids,
            "appointment_id": appointment_id,
            "laser_device_key": laser_device_key,
        }
    )


def _rows_for_ids(
    catalog: dict[str, Any],
    collection: str,
    ids: Iterable[str],
) -> list[dict[str, Any]]:
    wanted = list(dict.fromkeys(str(value) for value in ids))
    rank = {value: index for index, value in enumerate(wanted)}
    rows = catalog.get(collection)
    if not isinstance(rows, list):
        return []
    matched = [
        dict(item)
        for item in rows
        if isinstance(item, dict) and str(item.get("id")) in rank
    ]
    matched.sort(key=lambda item: rank[str(item.get("id"))])
    return matched


def grounded_catalog_facts(
    *,
    catalog: dict[str, Any],
    entity_hints: Any,
    capabilities: Iterable[str],
) -> dict[str, Any] | None:
    """Select verified catalog rows using only LLM-returned canonical IDs.

    No customer text is inspected or matched here. The LLM chooses canonical IDs
    from the catalog and this function merely materializes those exact rows.
    """
    capability_set = set(capabilities)
    payload: dict[str, Any] = {
        "ok": True,
        "source": "clinic_adapter_catalog",
    }

    selected_service_id = getattr(entity_hints, "service_id", None)
    service_candidate_ids = list(getattr(entity_hints, "service_candidate_ids", []) or [])
    selected_branch_id = getattr(entity_hints, "branch_id", None)
    branch_candidate_ids = list(getattr(entity_hints, "branch_candidate_ids", []) or [])
    selected_doctor_id = getattr(entity_hints, "doctor_id", None)
    doctor_candidate_ids = list(getattr(entity_hints, "doctor_candidate_ids", []) or [])

    if (
        "doctor_discovery" in capability_set
        and "availability_discovery" not in capability_set
        and "appointment_creation" not in capability_set
        and selected_doctor_id is None
        and not doctor_candidate_ids
        and selected_service_id
    ):
        service_row = _catalog_row_by_id(catalog, "services", selected_service_id)
        known_doctor_ids = _catalog_ids(catalog, "doctors")
        if service_row is not None:
            doctor_candidate_ids = [
                str(value)
                for value in (service_row.get("doctor_ids") or [])
                if str(value) in known_doctor_ids
            ]

    if capability_set.intersection({"service_information", "pricing", "availability_discovery", "appointment_creation"}):
        service_ids = [selected_service_id] if selected_service_id else service_candidate_ids
        if service_ids:
            payload["services"] = _rows_for_ids(catalog, "services", service_ids)
            payload["selected_service_id"] = selected_service_id
            payload["needs_service_choice"] = selected_service_id is None and len(payload["services"]) > 1

    if capability_set.intersection({"branch_discovery", "availability_discovery", "appointment_creation"}):
        branch_ids = [selected_branch_id] if selected_branch_id else branch_candidate_ids
        if branch_ids:
            payload["branches"] = _rows_for_ids(catalog, "branches", branch_ids)
            payload["selected_branch_id"] = selected_branch_id
            payload["needs_branch_choice"] = selected_branch_id is None and len(payload["branches"]) > 1

    if capability_set.intersection({"doctor_discovery", "availability_discovery", "appointment_creation"}):
        doctor_ids = [selected_doctor_id] if selected_doctor_id else doctor_candidate_ids
        if doctor_ids:
            payload["doctors"] = _rows_for_ids(catalog, "doctors", doctor_ids)
            payload["selected_doctor_id"] = selected_doctor_id
            payload["needs_doctor_choice"] = selected_doctor_id is None and len(payload["doctors"]) > 1

    if len(payload) == 2:
        return None
    return payload


def choice_snapshot_from_grounded_facts(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    snapshot: dict[str, Any] = {}
    for flag, collection in (
        ("needs_service_choice", "services"),
        ("needs_branch_choice", "branches"),
        ("needs_doctor_choice", "doctors"),
    ):
        if payload.get(flag) and isinstance(payload.get(collection), list):
            snapshot[flag] = True
            snapshot[collection] = payload[collection]
    return snapshot or None