from __future__ import annotations

from app.agents.v2.semantic_context import SemanticContext


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
    return {**catalog, "branches": filtered}


def _branch_ids(catalog: dict[str, object]) -> set[str]:
    branches = catalog.get("branches")
    return (
        {
            str(row.get("id") or row.get("branch_id"))
            for row in branches
            if isinstance(row, dict)
            and (row.get("id") or row.get("branch_id")) not in (None, "")
        }
        if isinstance(branches, list)
        else set()
    )


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
    must clarify and must not perform an availability read/write. Contexts without
    any canonical branch facts retain legacy planning behavior.
    """
    raw_catalog = _catalog(context)
    if raw_catalog is None:
        return params, [], None

    explicit = str(params.get("branch_id") or "").strip()
    raw_branch_ids = _branch_ids(raw_catalog)
    if not raw_branch_ids:
        return params, [], None

    catalog = _compatible_catalog(
        raw_catalog,
        doctor_id=str(params.get("doctor_id") or "").strip() or None,
    )
    branch_ids = _branch_ids(catalog)

    if explicit:
        if branch_ids and explicit not in branch_ids:
            return params, _refs_for_branch_ids(context, branch_ids), "incompatible_branch"
        if not branch_ids and explicit in raw_branch_ids:
            return params, _refs_for_branch_ids(context, raw_branch_ids), "incompatible_branch"
        return params, [], None

    # A configured primary branch is not a customer branch preference. Scheduling
    # may auto-resolve only when the grounded compatible set itself contains one
    # branch; otherwise the customer must choose from verified branch refs.
    if len(branch_ids) == 1:
        return {**params, "branch_id": next(iter(branch_ids))}, [], None

    refs = _refs_for_branch_ids(context, branch_ids)
    return params, refs, "branch_choice" if refs else "no_compatible_branch"
