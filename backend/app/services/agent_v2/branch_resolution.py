from typing import Any


def resolve_single_location_branch_id(
    *,
    primary_branch_id: object,
    catalog: dict[str, Any],
) -> str | None:
    """Resolve a branch only when the clinic context is deterministic."""
    if primary_branch_id is not None:
        return str(primary_branch_id)

    rows = catalog.get("branches")
    if not isinstance(rows, list):
        return None
    branch_ids = [
        str(row["id"])
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]
    if len(branch_ids) == 1:
        return branch_ids[0]
    return None
