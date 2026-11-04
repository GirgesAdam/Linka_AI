from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models.patient import Patient
from app.services import historical_import as history


def _holding_row(index: int, customer_id: str | None = None) -> SimpleNamespace:
    phone = f"010{index:08d}"
    return SimpleNamespace(
        entity_type="holding",
        normalized_json={
            "import_contract": history.ACTIVE_HOLDING_CONTRACT,
            "patient_phone_normalized": phone,
            "patient_phone": phone,
            "customer_id": customer_id,
        },
        row_status="ready",
        row_number=index + 2,
        issue_code=None,
        issue_message=None,
    )


@pytest.mark.parametrize(
    ("row_count", "expected_patient_selects"),
    [(1, 1), (100, 1), (901, 2), (1545, 2)],
)
def test_active_holding_patient_lookup_query_count_is_chunked(
    row_count: int,
    expected_patient_selects: int,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Patient.__table__.create(engine)
    workspace_id = uuid4()
    rows = [_holding_row(index) for index in range(row_count)]
    query_count = 0

    def count_patient_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal query_count
        if statement.lstrip().upper().startswith("SELECT") and "from patients" in statement.lower():
            query_count += 1

    event.listen(engine, "before_cursor_execute", count_patient_selects)
    try:
        with Session(engine) as db:
            history._validate_active_holding_customer_ids(
                db,
                workspace_id=workspace_id,
                rows=rows,
                ready_counts=Counter({"holding": row_count}),
                rejected_counts=Counter(),
                issue_counts={},
            )
    finally:
        event.remove(engine, "before_cursor_execute", count_patient_selects)
        engine.dispose()

    assert query_count == expected_patient_selects
    assert all(row.row_status == "ready" for row in rows)


def test_active_holding_batch_lookup_preserves_customer_id_conflicts_and_workspace_scope() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Patient.__table__.create(engine)
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    rows = [
        _holding_row(0, "IMPORTED-0"),
        _holding_row(1, "IMPORTED-1"),
        _holding_row(2),
    ]
    ready_counts = Counter({"holding": len(rows)})
    rejected_counts: Counter[str] = Counter()
    issue_counts = {}

    try:
        with Session(engine) as db:
            db.add_all(
                [
                    Patient(
                        workspace_id=workspace_id,
                        first_name="Same workspace",
                        phone_normalized="01000000000",
                        customer_id="CURRENT-0",
                    ),
                    Patient(
                        workspace_id=other_workspace_id,
                        first_name="Other workspace",
                        phone_normalized="01000000001",
                        customer_id="OTHER-1",
                    ),
                ]
            )
            db.flush()
            history._validate_active_holding_customer_ids(
                db,
                workspace_id=workspace_id,
                rows=rows,
                ready_counts=ready_counts,
                rejected_counts=rejected_counts,
                issue_counts=issue_counts,
            )
    finally:
        engine.dispose()

    assert rows[0].row_status == "rejected"
    assert rows[0].issue_code == "customer_id_conflict_existing_patient"
    assert rows[1].row_status == "ready"
    assert rows[2].row_status == "ready"
    assert ready_counts["holding"] == 2
    assert rejected_counts["holding"] == 1
    assert any(key[1] == "customer_id_conflict_existing_patient" for key in issue_counts)
