from __future__ import annotations

from app.agents.v2.semantic_context import SemanticContext
from app.services.agent_v2.branch_resolution import resolve_single_location_branch_id


def _catalog(context: SemanticContext) -> dict[str, object] | None:
    raw = context.server_metadata.get("branch_resolution_catalog")
    return dict(raw) if isinstance(raw, dict) else None


def _compatible_catalog(
    catalog: dict[str, object],
    *,
    doctor_id: str | None,
) -> dict[str, object]:
    if not doctor_id:
        return catalog
    doctors = catalog.get("doctors")
    allowed: set[str] | None = None
    if isinstance(doctors, list):
        for row in doctors:
            if not isinstance(row, dict) or str(row.get("id") or "") != doctor_id:
                continue
            raw = row.get("branch_ids")
            allowed = (
                {str(value) for value in raw if value not in (None, "")}
                if isinstance(raw, list)
                else set()
            )
            break
    if allowed is None:
        return catalog
    branches = catalog.get("branches")
    filtered = (
        [
            dict(row)
            for row in branches
            if isinstance(row, dict)
            and str(row.get("id") or row.get("branch_id") or "") in allowed
        ]
        if isinstance(branches, list)
        else []
    )
    primary = catalog.get("primary_branch_id")
    if primary not in (None, "") and str(primary) not in allowed:
        primary = None
    return {**catalog, "primary_branch_id": primary, "branches": filtered}


def _refs_for_branch_ids(context: SemanticContext, branch_ids: set[str]) -> list[str]:
    return [
        ref
        for ref, target in context.reference_map.items()
        if target.kind == "branch" and target.canonical_id in branch_ids
    ]


def resolve_scheduling_branch_parameters(
    params: dict[str, object],
    *,
    context: SemanticContext,
) -> tuple[dict[str, object], list[str], str | None]:
    """Resolve branch only from canonical backend facts; never from customer text.

    Returns (parameters, candidate_refs, reason). A non-null reason means the caller
    must clarify and must not perform an availability read/write. Contexts that do
    not provide canonical branch metadata retain legacy planning behavior.
    """
    raw_catalog = _catalog(context)
    if raw_catalog is None:
        return params, [], None

    explicit = str(params.get("branch_id") or "").strip()
    catalog = _compatible_catalog(
        raw_catalog,
        doctor_id=str(params.get("doctor_id") or "").strip() or None,
    )
    branches = catalog.get("branches")
    branch_ids = (
        {
            str(row.get("id") or row.get("branch_id"))
            for row in branches
            if isinstance(row, dict)
            and (row.get("id") or row.get("branch_id")) not in (None, "")
        }
        if isinstance(branches, list)
        else set()
    )

    if explicit:
        if branch_ids and explicit not in branch_ids:
            return params, _refs_for_branch_ids(context, branch_ids), "incompatible_branch"
        return params, [], None

    resolved = resolve_single_location_branch_id(
        primary_branch_id=catalog.get("primary_branch_id"),
        catalog=catalog,
    )
    if resolved is None:
        refs = _refs_for_branch_ids(context, branch_ids)
        return params, refs, "branch_choice" if refs else "no_compatible_branch"
    return {**params, "branch_id": resolved}, [], None
