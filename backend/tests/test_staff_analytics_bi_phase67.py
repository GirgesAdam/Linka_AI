from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.schemas.analytics_bi import AnalyticsBIPlan
from app.services.analytics_bi import (
    AnalyticsBIError,
    analytics_entity_catalog,
    execute_analytics_plan,
    validate_analytics_plan_entities,
)

NOW = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)


def _schema(engine) -> None:
    ddl = [
        """
        CREATE TABLE patients (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            first_name VARCHAR(120), last_name VARCHAR(120), phone VARCHAR(40),
            phone_normalized VARCHAR(40), customer_id TEXT, gender VARCHAR(32), birth_date DATE,
            preferred_language VARCHAR(10), preferred_branch_id CHAR(32), source VARCHAR(32),
            source_detail VARCHAR(200), status VARCHAR(20), marketing_consent BOOLEAN DEFAULT 0,
            marketing_consent_at DATETIME, whatsapp_opt_in BOOLEAN DEFAULT 0,
            whatsapp_opt_in_at DATETIME, whatsapp_opt_in_source VARCHAR(32), source_created_at DATETIME, last_contact_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE services (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            name VARCHAR(180) NOT NULL, category VARCHAR(120), description TEXT,
            duration_minutes INTEGER DEFAULT 60, buffer_before_minutes INTEGER DEFAULT 0,
            buffer_after_minutes INTEGER DEFAULT 0, price_minor INTEGER DEFAULT 0,
            currency VARCHAR(3) DEFAULT 'EGP', requires_medical_review BOOLEAN DEFAULT 0,
            requires_laser_device BOOLEAN DEFAULT 0, is_active BOOLEAN DEFAULT 1, created_at DATETIME, updated_at DATETIME
        )
        """,
        """
        CREATE TABLE branches (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL, name VARCHAR(180) NOT NULL,
            code VARCHAR(80), phone VARCHAR(40), city VARCHAR(120), address_line1 VARCHAR(255),
            address_line2 VARCHAR(255), timezone VARCHAR(80), is_active BOOLEAN DEFAULT 1,
            created_at DATETIME, updated_at DATETIME
        )
        """,
        """
        CREATE TABLE staff (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL,
            user_id CHAR(32), first_name VARCHAR(120), last_name VARCHAR(120), email VARCHAR(320),
            phone VARCHAR(40), job_title VARCHAR(160), is_active BOOLEAN DEFAULT 1,
            created_at DATETIME, updated_at DATETIME
        )
        """,
        """
        CREATE TABLE doctors (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32) NOT NULL, staff_id CHAR(32) NOT NULL,
            specialization VARCHAR(200), license_number VARCHAR(120), bio VARCHAR(2000),
            booking_enabled BOOLEAN DEFAULT 1, is_active BOOLEAN DEFAULT 1,
            created_at DATETIME, updated_at DATETIME
        )
        """,
        """
        CREATE TABLE appointments (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), patient_id CHAR(32),
            branch_id CHAR(32), doctor_id CHAR(32), doctor_assignment_known BOOLEAN DEFAULT 1, service_id CHAR(32), patient_package_id CHAR(32), visit_group_id CHAR(32), lead_id CHAR(32),
            created_by_user_id CHAR(32), rescheduled_from_appointment_id CHAR(32),
            status VARCHAR(20), source VARCHAR(20), start_at DATETIME, end_at DATETIME,
            busy_start_at DATETIME, busy_end_at DATETIME, duration_minutes INTEGER,
            price_minor INTEGER, discount_minor INTEGER DEFAULT 0, is_quick_booking BOOLEAN DEFAULT 0, laser_pulses_used INTEGER, currency VARCHAR(3), laser_device_key VARCHAR(40), laser_device_name VARCHAR(120), payment_status VARCHAR(16),
            amount_paid_minor INTEGER, payment_method VARCHAR(20), billing_context VARCHAR(24) DEFAULT 'standard', package_external_id VARCHAR(128), customer_note TEXT,
            cancellation_reason TEXT, idempotency_key VARCHAR(128), confirmed_at DATETIME,
            cancelled_at DATETIME, completed_at DATETIME, no_show_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE payment_transactions (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), appointment_id CHAR(32),
            origin_appointment_id CHAR(32), patient_id CHAR(32), created_by_user_id CHAR(32),
            reference_transaction_id CHAR(32), patient_package_id CHAR(32), patient_pulse_pack_id CHAR(32), transaction_type VARCHAR(16), amount_minor INTEGER,
            currency VARCHAR(3), payment_method VARCHAR(24), source VARCHAR(24),
            external_reference VARCHAR(128), reason TEXT, idempotency_key VARCHAR(128),
            created_at DATETIME
        )
        """,
        """
        CREATE TABLE payment_allocations (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), transaction_id CHAR(32),
            appointment_id CHAR(32), amount_minor INTEGER, created_at DATETIME
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)


def _insert_appointment(db: Session, *, ids: dict, patient, service, start: datetime, status="completed", price=100000):
    appointment_id = uuid4()
    end = start.replace(hour=min(start.hour + 1, 23))
    db.execute(
        text(
            "INSERT INTO appointments "
            "(id,workspace_id,patient_id,branch_id,doctor_id,service_id,status,source,start_at,end_at,busy_start_at,busy_end_at,duration_minutes,price_minor,currency,payment_status,payment_method,created_at,updated_at) "
            "VALUES (:id,:w,:p,:b,:d,:s,:status,'integration',:start,:end,:start,:end,60,:price,'EGP','paid','cash',:start,:start)"
        ),
        {
            "id": appointment_id.hex,
            "w": ids["workspace"].hex,
            "p": patient.hex,
            "b": ids["branch"].hex,
            "d": ids["doctor"].hex,
            "s": service.hex,
            "status": status,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "price": price,
        },
    )
    return appointment_id


def _pay(db: Session, *, ids: dict, patient, appointment, amount: int, at: datetime, refund: bool = False):
    tx = uuid4()
    tx_type = "refund" if refund else "payment"
    db.execute(
        text(
            "INSERT INTO payment_transactions "
            "(id,workspace_id,appointment_id,origin_appointment_id,patient_id,reference_transaction_id,transaction_type,amount_minor,currency,payment_method,source,created_at) "
            "VALUES (:id,:w,:a,:a,:p,:ref,:kind,:amount,'EGP','cash','integration',:at)"
        ),
        {
            "id": tx.hex,
            "w": ids["workspace"].hex,
            "a": appointment.hex,
            "p": patient.hex,
            "ref": uuid4().hex if refund else None,
            "kind": tx_type,
            "amount": amount,
            "at": at.isoformat(),
        },
    )
    db.execute(
        text(
            "INSERT INTO payment_allocations (id,workspace_id,transaction_id,appointment_id,amount_minor,created_at) "
            "VALUES (:id,:w,:t,:a,:amount,:at)"
        ),
        {
            "id": uuid4().hex,
            "w": ids["workspace"].hex,
            "t": tx.hex,
            "a": appointment.hex,
            "amount": amount,
            "at": at.isoformat(),
        },
    )


def _seed(db: Session) -> dict:
    ids = {
        "workspace": uuid4(),
        "laser": uuid4(),
        "prp": uuid4(),
        "branch": uuid4(),
        "doctor": uuid4(),
        "staff": uuid4(),
        "p1": uuid4(),
        "p2": uuid4(),
        "p3": uuid4(),
    }
    w = ids["workspace"].hex
    db.execute(text("INSERT INTO services (id,workspace_id,name,is_active) VALUES (:id,:w,'Laser',1)"), {"id": ids["laser"].hex, "w": w})
    db.execute(text("INSERT INTO services (id,workspace_id,name,is_active) VALUES (:id,:w,'PRP',1)"), {"id": ids["prp"].hex, "w": w})
    db.execute(text("INSERT INTO branches (id,workspace_id,name,is_active) VALUES (:id,:w,'New Cairo',1)"), {"id": ids["branch"].hex, "w": w})
    db.execute(text("INSERT INTO staff (id,workspace_id,first_name,last_name,is_active) VALUES (:id,:w,'Sara','Hassan',1)"), {"id": ids["staff"].hex, "w": w})
    db.execute(text("INSERT INTO doctors (id,workspace_id,staff_id,is_active,booking_enabled) VALUES (:id,:w,:staff,1,1)"), {"id": ids["doctor"].hex, "w": w, "staff": ids["staff"].hex})

    patients = [
        (ids["p1"], "Mona", "Ali", "01000000001", "2022-01-10T09:00:00+00:00"),
        (ids["p2"], "Nour", "Samy", "01000000002", "2023-02-10T09:00:00+00:00"),
        (ids["p3"], "Laila", "Omar", "01000000003", "2026-07-10T09:00:00+00:00"),
    ]
    for pid, first, last, phone, source_created in patients:
        db.execute(
            text(
                "INSERT INTO patients (id,workspace_id,first_name,last_name,phone,phone_normalized,preferred_language,source,status,marketing_consent,source_created_at,created_at,updated_at) "
                "VALUES (:id,:w,:first,:last,:phone,:phone,'ar','other','active',0,:source_created,'2026-08-26T00:00:00+00:00','2026-08-26T00:00:00+00:00')"
            ),
            {"id": pid.hex, "w": w, "first": first, "last": last, "phone": phone, "source_created": source_created},
        )

    # P1: three completed Laser visits, recent enough to stay active.
    a11 = _insert_appointment(db, ids=ids, patient=ids["p1"], service=ids["laser"], start=datetime(2026, 1, 10, 10, tzinfo=UTC), price=100000)
    a12 = _insert_appointment(db, ids=ids, patient=ids["p1"], service=ids["laser"], start=datetime(2026, 4, 10, 10, tzinfo=UTC), price=120000)
    a13 = _insert_appointment(db, ids=ids, patient=ids["p1"], service=ids["laser"], start=datetime(2026, 8, 10, 10, tzinfo=UTC), price=130000)
    _pay(db, ids=ids, patient=ids["p1"], appointment=a11, amount=100000, at=datetime(2026, 1, 10, 11, tzinfo=UTC))
    _pay(db, ids=ids, patient=ids["p1"], appointment=a12, amount=120000, at=datetime(2026, 4, 10, 11, tzinfo=UTC))
    _pay(db, ids=ids, patient=ids["p1"], appointment=a13, amount=130000, at=datetime(2026, 8, 10, 11, tzinfo=UTC))

    # P2: two Laser visits, last one old enough to be lapsed.
    a21 = _insert_appointment(db, ids=ids, patient=ids["p2"], service=ids["laser"], start=datetime(2025, 6, 10, 10, tzinfo=UTC), price=80000)
    a22 = _insert_appointment(db, ids=ids, patient=ids["p2"], service=ids["laser"], start=datetime(2025, 10, 10, 10, tzinfo=UTC), price=90000)
    _pay(db, ids=ids, patient=ids["p2"], appointment=a21, amount=80000, at=datetime(2025, 6, 10, 11, tzinfo=UTC))
    _pay(db, ids=ids, patient=ids["p2"], appointment=a22, amount=90000, at=datetime(2025, 10, 10, 11, tzinfo=UTC))

    # P3: one recent PRP visit and a partial refund.
    a31 = _insert_appointment(db, ids=ids, patient=ids["p3"], service=ids["prp"], start=datetime(2026, 8, 20, 10, tzinfo=UTC), price=200000)
    _pay(db, ids=ids, patient=ids["p3"], appointment=a31, amount=200000, at=datetime(2026, 8, 20, 11, tzinfo=UTC))
    _pay(db, ids=ids, patient=ids["p3"], appointment=a31, amount=50000, at=datetime(2026, 8, 21, 11, tzinfo=UTC), refund=True)

    db.commit()
    return ids


def _plan(operation: str, **updates) -> AnalyticsBIPlan:
    payload = {
        "operation": operation,
        "lookback_days": None,
        "inactivity_days": None,
        "limit": 10,
        "service_ids": [],
        "branch_ids": [],
        "doctor_ids": [],
        "currency": None,
        "patient_name": None,
        "patient_phone": None,
        "reason": "test",
    }
    payload.update(updates)
    return AnalyticsBIPlan.model_validate(payload)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _schema(engine)
    return engine


def test_service_retention_has_fixed_same_service_definition() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        result = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="ايه الخدمات اللي retention بتاعها اعلى؟",
            plan=_plan("service_retention"),
            now=NOW,
        )
        assert result.rows[0].label == "Laser"
        metrics = {item.key: item.value for item in result.rows[0].metrics}
        assert metrics["service_repeat_rate"] == 100.0
        assert metrics["repeat_patients"] == 2
        assert "2+" in result.definitions[0]


def test_lapsed_patients_excludes_recent_customers_and_uses_last_completed_visit() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        result = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="مين العملاء اللي اختفوا من 6 شهور؟",
            plan=_plan("lapsed_patients", inactivity_days=180),
            now=NOW,
        )
        assert [row.label for row in result.rows] == ["Nour Samy"]
        assert result.rows[0].secondary_label.startswith("01000000002")
        assert "180+" in result.answer


def test_repeat_and_value_rankings_are_patient_level_and_bounded() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        repeat = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="مين اكتر customers بيرجعوا؟",
            plan=_plan("top_repeat_patients", limit=2),
            now=NOW,
        )
        assert repeat.rows[0].label == "Mona Ali"
        assert repeat.rows[0].metrics[0].value == 3

        value = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="مين اعلى العملاء قيمة؟",
            plan=_plan("top_value_patients", limit=2),
            now=NOW,
        )
        assert value.rows[0].label == "Mona Ali"
        assert value.rows[0].metrics[0].value == 350000
        assert value.rows[0].metrics[0].currency == "EGP"


def test_entity_filtered_revenue_uses_explicit_payment_allocations_only() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        result = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="ايرادات الليزر",
            plan=_plan("revenue_trend", service_ids=[str(ids["laser"])]),
            now=NOW,
        )
        total = sum(int(row.metrics[0].value) for row in result.rows)
        assert total == 520000
        assert all(row.metrics[0].currency == "EGP" for row in result.rows)
        assert "allocations" in result.definitions[1]


def test_new_patient_trend_uses_source_created_at_not_import_date() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        result = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="العملاء الجدد اخر 90 يوم",
            plan=_plan("new_patients_trend", lookback_days=90),
            now=NOW,
        )
        total = sum(int(row.metrics[0].value) for row in result.rows)
        assert total == 1
        assert result.rows[0].label == "2026-07-10"


def test_planner_entity_ids_fail_closed_against_canonical_catalog() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        catalog = analytics_entity_catalog(db, workspace_id=ids["workspace"])
        assert {item["name"] for item in catalog["services"]} == {"Laser", "PRP"}
        assert catalog["doctors"][0]["name"] == "Sara Hassan"
        valid = _plan("service_performance", service_ids=[str(ids["laser"])])
        assert validate_analytics_plan_entities(valid, catalog=catalog) is valid
        with pytest.raises(AnalyticsBIError, match="unknown canonical service"):
            validate_analytics_plan_entities(
                _plan("service_performance", service_ids=[str(uuid4())]),
                catalog=catalog,
            )
        with pytest.raises(AnalyticsBIError, match="does not accept"):
            validate_analytics_plan_entities(
                _plan("new_patients_trend", service_ids=[str(ids["laser"])]),
                catalog=catalog,
            )
        with pytest.raises(AnalyticsBIError, match="only valid"):
            validate_analytics_plan_entities(
                _plan("service_retention", inactivity_days=180),
                catalog=catalog,
            )



def test_staff_can_read_one_patient_history_by_phone_or_unique_exact_name() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        by_phone = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="مونا علي عملت ايه عندنا؟",
            plan=_plan("patient_history_lookup", patient_phone="01000000001"),
            now=NOW,
        )
        assert by_phone.rows[0].label == "Mona Ali"
        assert any(row.label == "Laser" for row in by_phone.rows)
        assert any(row.label == "مدفوعات EGP" for row in by_phone.rows)

        by_name = execute_analytics_plan(
            db,
            workspace_id=ids["workspace"],
            question="Nour Samy عمل ايه؟",
            plan=_plan("patient_history_lookup", patient_name="Nour Samy"),
            now=NOW,
        )
        assert by_name.rows[0].label == "Nour Samy"
        assert "2 زيارة مكتملة" in by_name.answer


def test_patient_name_lookup_fails_closed_when_exact_name_is_ambiguous() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        db.execute(
            text(
                "INSERT INTO patients (id,workspace_id,first_name,last_name,phone,phone_normalized,preferred_language,source,status,marketing_consent,source_created_at,created_at,updated_at) "
                "VALUES (:id,:w,'Nour','Samy','01000000199','01000000199','ar','other','active',0,'2024-01-01T00:00:00+00:00','2026-08-26T00:00:00+00:00','2026-08-26T00:00:00+00:00')"
            ),
            {"id": uuid4().hex, "w": ids["workspace"].hex},
        )
        db.commit()
        with pytest.raises(AnalyticsBIError, match="Multiple patients match"):
            execute_analytics_plan(
                db,
                workspace_id=ids["workspace"],
                question="Nour Samy عمل ايه؟",
                plan=_plan("patient_history_lookup", patient_name="Nour Samy"),
                now=NOW,
            )

def test_bi_architecture_uses_typed_planner_and_no_arbitrary_sql_or_patient_rows_in_prompt() -> None:
    root = Path(__file__).resolve().parents[1]
    planner = (root / "app/agents/analytics_planner.py").read_text(encoding="utf-8").lower()
    service = (root / "app/services/analytics_bi.py").read_text(encoding="utf-8").lower()
    route = (root / "app/api/routes/analytics.py").read_text(encoding="utf-8")
    schema = (root / "app/schemas/analytics_bi.py").read_text(encoding="utf-8")
    assert "analyticssbiplan" not in schema.lower()  # typo guard: contract name stays stable.
    assert "class AnalyticsBIPlan" in schema
    assert "do not write sql" in planner
    assert "patient rows" not in planner
    # The typed planner remains covered as internal/legacy code, but Phase 7.7
    # retires the public free-text analytics endpoints from the product surface.
    assert "analytics_planner" not in route
    assert 'router.post("/ask"' not in route
    assert 'router.post("/compose"' not in route
    assert "langchain" not in service
    assert "eval(" not in service and "exec(" not in service
    assert "from sqlalchemy import text" not in service and ".execute(text(" not in service
