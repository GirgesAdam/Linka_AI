from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies.security import get_current_user
from app.api.routes.booking import router as booking_router
from app.api.routes.booking import workspace_timezone
from app.api.routes.crm import router as crm_router
from app.database.base import Base
from app.database.session import get_db
from app.models.branch import Branch
from app.models.patient import Patient
from app.models.user import User
from app.models.working_hours import BranchWorkingHour
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember


def _database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Workspace.__table__,
            WorkspaceMember.__table__,
            Branch.__table__,
            BranchWorkingHour.__table__,
            Patient.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        workspace_a = Workspace(
            name="Workspace A",
            slug=f"workspace-a-{uuid4().hex}",
            timezone="Africa/Cairo",
        )
        workspace_b = Workspace(
            name="Workspace B",
            slug=f"workspace-b-{uuid4().hex}",
            timezone="Europe/London",
        )
        actor_a = User(
            email=f"member-a-{uuid4()}@example.test",
            auth_user_id=uuid4(),
        )
        actor_b = User(
            email=f"member-b-{uuid4()}@example.test",
            auth_user_id=uuid4(),
        )
        actor_inactive = User(
            email=f"member-inactive-{uuid4()}@example.test",
            auth_user_id=uuid4(),
        )
        db.add_all([workspace_a, workspace_b, actor_a, actor_b, actor_inactive])
        db.flush()

        db.add_all(
            [
                WorkspaceMember(
                    workspace_id=workspace_a.id,
                    user_id=actor_a.id,
                    role="member",
                    is_active=True,
                    section_access={
                        "clinic_operations": False,
                        "administration": False,
                    },
                    permissions_version=0,
                ),
                WorkspaceMember(
                    workspace_id=workspace_b.id,
                    user_id=actor_b.id,
                    role="member",
                    is_active=True,
                    section_access={
                        "clinic_operations": False,
                        "administration": False,
                    },
                    permissions_version=0,
                ),
                WorkspaceMember(
                    workspace_id=workspace_a.id,
                    user_id=actor_inactive.id,
                    role="member",
                    is_active=False,
                    section_access={
                        "clinic_operations": False,
                        "administration": False,
                    },
                    permissions_version=0,
                ),
            ]
        )

        branch_a = Branch(
            workspace_id=workspace_a.id,
            name="Cairo Main",
            code="CAI",
            phone="01000000000",
            email="private@example.test",
            address_line1="Private branch address",
            timezone=None,
            is_active=True,
        )
        branch_b = Branch(
            workspace_id=workspace_b.id,
            name="London Branch",
            code="LON",
            timezone="Europe/Paris",
            is_active=True,
        )
        inactive_branch_a = Branch(
            workspace_id=workspace_a.id,
            name="Inactive Branch",
            code="CAI-OLD",
            is_active=False,
        )
        db.add_all([branch_a, branch_b, inactive_branch_a])
        db.flush()

        db.add_all(
            [
                BranchWorkingHour(
                    workspace_id=workspace_a.id,
                    branch_id=branch_a.id,
                    weekday=0,
                    start_time=time(9, 0),
                    end_time=time(12, 0),
                ),
                BranchWorkingHour(
                    workspace_id=workspace_a.id,
                    branch_id=branch_a.id,
                    weekday=0,
                    start_time=time(13, 0),
                    end_time=time(17, 0),
                ),
                BranchWorkingHour(
                    workspace_id=workspace_b.id,
                    branch_id=branch_b.id,
                    weekday=1,
                    start_time=time(10, 0),
                    end_time=time(18, 0),
                ),
            ]
        )

        patient_a_old = Patient(
            workspace_id=workspace_a.id,
            first_name="Salma",
            last_name="Old",
            phone="01011111111",
            gender="female",
            birth_date=date(1980, 1, 1),
            marketing_consent=True,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        patient_a_new = Patient(
            workspace_id=workspace_a.id,
            first_name="Salma",
            last_name="Hassan",
            phone="01022222222",
            gender="female",
            birth_date=date(1990, 2, 2),
            marketing_consent=True,
            created_at=datetime(2025, 1, 2, tzinfo=UTC),
            updated_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        patient_b = Patient(
            workspace_id=workspace_b.id,
            first_name="Salma",
            last_name="Other Workspace",
            phone="01033333333",
        )
        db.add_all([patient_a_old, patient_a_new, patient_b])
        db.commit()

        ids = {
            "workspace_a": workspace_a.id,
            "workspace_b": workspace_b.id,
            "actor_a_id": actor_a.id,
            "actor_b_id": actor_b.id,
            "actor_inactive_id": actor_inactive.id,
            "branch_a": branch_a.id,
            "branch_b": branch_b.id,
            "patient_a_old": patient_a_old.id,
            "patient_a_new": patient_a_new.id,
            "patient_b": patient_b.id,
        }
    return engine, factory, ids


def _database_dependency(factory):
    def override_get_db():
        with factory() as db:
            yield db

    return override_get_db


def _member_client(factory, ids, actor_key="actor_a_id"):
    app = FastAPI()
    app.include_router(booking_router, prefix="/booking")
    app.include_router(crm_router, prefix="/crm")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=ids[actor_key]
    )
    app.dependency_overrides[get_db] = _database_dependency(factory)
    return TestClient(app)


def test_workspace_timezone_uses_first_valid_branch_workspace_or_cairo_candidate():
    scenarios = (
        ("Asia/Riyadh", "Europe/London", "Asia/Riyadh"),
        ("Invalid/Zone", "Europe/London", "Europe/London"),
        (None, "Europe/London", "Europe/London"),
        ("Invalid/Zone", "Invalid/Workspace", "Africa/Cairo"),
        (None, "Invalid/Workspace", "Africa/Cairo"),
        ("/etc/passwd", "Europe/London", "Europe/London"),
        (None, "/etc/passwd", "Africa/Cairo"),
        ("America/New_York", "Asia/Dubai", "America/New_York"),
    )

    for branch_name, workspace_name, expected in scenarios:
        access = SimpleNamespace(workspace=SimpleNamespace(timezone=workspace_name))
        branch = SimpleNamespace(timezone=branch_name) if branch_name is not None else None

        assert workspace_timezone(access, branch).key == expected


def test_branch_schedules_use_valid_workspace_timezone_when_branch_timezone_is_invalid():
    engine, factory, ids = _database()
    try:
        with factory() as db:
            workspace = db.get(Workspace, ids["workspace_a"])
            branch = db.get(Branch, ids["branch_a"])
            assert workspace is not None
            assert branch is not None
            workspace.timezone = "Europe/London"
            branch.timezone = "Invalid/Zone"
            db.commit()

        client = _member_client(factory, ids)
        with client:
            response = client.get(
                "/booking/branch-schedules",
                headers={"X-Workspace-ID": str(ids["workspace_a"])},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["workspace_timezone"] == "Europe/London"
        assert payload["branches"][0]["timezone"] == "Europe/London"
    finally:
        engine.dispose()


def test_branch_schedules_are_workspace_scoped_minimal_and_timezone_aware():
    engine, factory, ids = _database()
    try:
        client = _member_client(factory, ids)
        with client:
            response = client.get(
                "/booking/branch-schedules",
                headers={"X-Workspace-ID": str(ids["workspace_a"])},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload == {
            "workspace_timezone": "Africa/Cairo",
            "branches": [
                {
                    "id": str(ids["branch_a"]),
                    "name": "Cairo Main",
                    "timezone": "Africa/Cairo",
                    "is_active": True,
                    "working_hours": [
                        {"weekday": 0, "start_time": "09:00:00", "end_time": "12:00:00"},
                        {"weekday": 0, "start_time": "13:00:00", "end_time": "17:00:00"},
                    ],
                }
            ],
        }
        branch_payload = payload["branches"][0]
        assert set(branch_payload) == {
            "id",
            "name",
            "timezone",
            "is_active",
            "working_hours",
        }
        assert "phone" not in branch_payload
        assert "email" not in branch_payload
        assert "address_line1" not in branch_payload

        with client:
            other_workspace_response = client.get(
                "/booking/branch-schedules",
                headers={"X-Workspace-ID": str(ids["workspace_b"])},
            )
        assert other_workspace_response.status_code == 403

        other_member_client = _member_client(factory, ids, "actor_b_id")
        with other_member_client:
            other_member_response = other_member_client.get(
                "/booking/branch-schedules",
                headers={"X-Workspace-ID": str(ids["workspace_b"])},
            )
        assert other_member_response.status_code == 200
        assert other_member_response.json() == {
            "workspace_timezone": "Europe/London",
            "branches": [
                {
                    "id": str(ids["branch_b"]),
                    "name": "London Branch",
                    "timezone": "Europe/Paris",
                    "is_active": True,
                    "working_hours": [
                        {"weekday": 1, "start_time": "10:00:00", "end_time": "18:00:00"}
                    ],
                }
            ],
        }
    finally:
        engine.dispose()


def test_branch_schedules_keep_workspace_timezone_without_active_branches_and_per_branch_timezones():
    engine, factory, ids = _database()
    try:
        with factory() as db:
            inactive_branch = db.get(Branch, ids["branch_b"])
            assert inactive_branch is not None
            inactive_branch.is_active = False
            db.add(
                Branch(
                    workspace_id=ids["workspace_a"],
                    name="Riyadh Branch",
                    code="RUH",
                    timezone="Asia/Riyadh",
                    is_active=True,
                )
            )
            db.commit()

        cairo_member = _member_client(factory, ids)
        with cairo_member:
            multiple_branches = cairo_member.get(
                "/booking/branch-schedules",
                headers={"X-Workspace-ID": str(ids["workspace_a"])},
            )
        assert multiple_branches.status_code == 200
        multiple_payload = multiple_branches.json()
        assert multiple_payload["workspace_timezone"] == "Africa/Cairo"
        assert {branch["name"]: branch["timezone"] for branch in multiple_payload["branches"]} == {
            "Cairo Main": "Africa/Cairo",
            "Riyadh Branch": "Asia/Riyadh",
        }

        london_member = _member_client(factory, ids, "actor_b_id")
        with london_member:
            no_active_branches = london_member.get(
                "/booking/branch-schedules",
                headers={"X-Workspace-ID": str(ids["workspace_b"])},
            )
        assert no_active_branches.status_code == 200
        assert no_active_branches.json() == {
            "workspace_timezone": "Europe/London",
            "branches": [],
        }
    finally:
        engine.dispose()


def test_patient_lookup_search_is_paginated_and_returns_only_required_fields():
    engine, factory, ids = _database()
    try:
        client = _member_client(factory, ids)
        headers = {"X-Workspace-ID": str(ids["workspace_a"])}
        with client:
            first_page = client.get(
                "/crm/patients/lookup",
                params={"q": "Salma", "limit": 1, "include_phone": "true"},
                headers=headers,
            )
            second_page = client.get(
                "/crm/patients/lookup",
                params={"q": "Salma", "limit": 1, "offset": 1},
                headers=headers,
            )

        assert first_page.status_code == 200
        assert second_page.status_code == 200
        first_payload = first_page.json()
        second_payload = second_page.json()
        assert len(first_payload) == 1
        assert len(second_payload) == 1
        assert first_payload[0]["id"] == str(ids["patient_a_new"])
        assert second_payload[0]["id"] == str(ids["patient_a_old"])
        assert set(first_payload[0]) == {"id", "first_name", "last_name", "phone"}
        assert first_payload[0] == {
            "id": str(ids["patient_a_new"]),
            "first_name": "Salma",
            "last_name": "Hassan",
            "phone": "01022222222",
        }
        assert "workspace_id" not in first_payload[0]
        assert "birth_date" not in first_payload[0]
        assert "gender" not in first_payload[0]
        assert "marketing_consent" not in first_payload[0]
    finally:
        engine.dispose()


def test_patient_lookup_omits_phone_from_default_sql_projection():
    engine, factory, ids = _database()
    statements: list[str] = []

    def capture_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        client = _member_client(factory, ids)
        with client:
            response = client.get(
                "/crm/patients/lookup",
                params={"q": "Salma"},
                headers={"X-Workspace-ID": str(ids["workspace_a"])},
            )

        assert response.status_code == 200
        assert response.json()[0]["phone"] is None
        patient_selects = [statement for statement in statements if "from patients" in statement.lower()]
        assert patient_selects
        projection = patient_selects[-1].lower().split("from patients", 1)[0]
        assert "patients.phone" not in projection
        assert ".phone" not in projection
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)
        engine.dispose()


def test_patient_lookup_batch_ids_never_return_another_workspaces_patient():
    engine, factory, ids = _database()
    try:
        client = _member_client(factory, ids)
        with client:
            own_workspace = client.post(
                "/crm/patients/lookup/batch",
                json={
                    "patient_ids": [str(ids["patient_a_new"]), str(ids["patient_b"])],
                },
                headers={"X-Workspace-ID": str(ids["workspace_a"])},
            )
            selected_patient = client.post(
                "/crm/patients/lookup/batch",
                json={
                    "patient_ids": [str(ids["patient_a_new"])],
                    "include_phone": True,
                },
                headers={"X-Workspace-ID": str(ids["workspace_a"])},
            )
        other_member_client = _member_client(factory, ids, "actor_b_id")
        other_workspace = other_member_client.post(
            "/crm/patients/lookup/batch",
            json={"patient_ids": [str(ids["patient_a_new"])]},
            headers={"X-Workspace-ID": str(ids["workspace_b"])},
        )

        assert own_workspace.status_code == 200
        assert [row["id"] for row in own_workspace.json()] == [str(ids["patient_a_new"])]
        assert own_workspace.json()[0]["phone"] is None
        assert selected_patient.status_code == 200
        assert selected_patient.json()[0]["phone"] == "01022222222"
        assert other_workspace.status_code == 200
        assert other_workspace.json() == []
    finally:
        engine.dispose()


def test_patient_lookup_batch_enforces_id_limit_uuid_validation_and_deduplication():
    engine, factory, ids = _database()
    try:
        client = _member_client(factory, ids)
        headers = {"X-Workspace-ID": str(ids["workspace_a"])}
        two_hundred_ids = [str(uuid4()) for _ in range(199)] + [str(ids["patient_a_new"])]
        two_hundred_one_ids = [str(uuid4()) for _ in range(201)]
        with client:
            one_id = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": [str(ids["patient_a_new"])]},
                headers=headers,
            )
            two_hundred = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": two_hundred_ids},
                headers=headers,
            )
            two_hundred_one = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": two_hundred_one_ids},
                headers=headers,
            )
            invalid_uuid = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": ["not-a-uuid"]},
                headers=headers,
            )
            duplicates = client.post(
                "/crm/patients/lookup/batch",
                json={
                    "patient_ids": [
                        str(ids["patient_a_new"]),
                        str(ids["patient_a_new"]),
                        str(ids["patient_a_new"]),
                    ]
                },
                headers=headers,
            )

        assert one_id.status_code == 200
        assert [row["id"] for row in one_id.json()] == [str(ids["patient_a_new"])]
        assert two_hundred.status_code == 200
        assert [row["id"] for row in two_hundred.json()] == [str(ids["patient_a_new"])]
        assert two_hundred_one.status_code == 422
        assert invalid_uuid.status_code == 422
        assert duplicates.status_code == 200
        assert [row["id"] for row in duplicates.json()] == [str(ids["patient_a_new"])]
    finally:
        engine.dispose()


def test_lookup_endpoints_reject_inactive_workspace_membership():
    engine, factory, ids = _database()
    try:
        client = _member_client(factory, ids, "actor_inactive_id")
        headers = {"X-Workspace-ID": str(ids["workspace_a"])}
        with client:
            branch_response = client.get(
                "/booking/branch-schedules",
                headers=headers,
            )
            patient_response = client.get(
                "/crm/patients/lookup",
                params={"q": "Salma"},
                headers=headers,
            )
            batch_patient_response = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": [str(ids["patient_a_new"])]},
                headers=headers,
            )

        assert branch_response.status_code == 403
        assert patient_response.status_code == 403
        assert batch_patient_response.status_code == 403
    finally:
        engine.dispose()


def test_patient_lookup_rejects_empty_search_and_invalid_batch_payloads():
    engine, factory, ids = _database()
    try:
        client = _member_client(factory, ids)
        headers = {"X-Workspace-ID": str(ids["workspace_a"])}
        with client:
            missing_query = client.get("/crm/patients/lookup", headers=headers)
            blank_query = client.get(
                "/crm/patients/lookup",
                params={"q": "   "},
                headers=headers,
            )
            missing_ids = client.post(
                "/crm/patients/lookup/batch",
                json={},
                headers=headers,
            )
            empty_ids = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": []},
                headers=headers,
            )

        assert missing_query.status_code == 422
        assert blank_query.status_code == 422
        assert missing_ids.status_code == 422
        assert empty_ids.status_code == 422
    finally:
        engine.dispose()


def test_lookup_endpoints_require_authentication():
    engine, factory, ids = _database()
    try:
        app = FastAPI()
        app.include_router(booking_router, prefix="/booking")
        app.include_router(crm_router, prefix="/crm")
        app.dependency_overrides[get_db] = _database_dependency(factory)
        client = TestClient(app)
        headers = {"X-Workspace-ID": str(ids["workspace_a"])}

        with client:
            branch_response = client.get(
                "/booking/branch-schedules",
                headers=headers,
            )
            patient_response = client.get(
                "/crm/patients/lookup",
                params={"q": "Salma"},
                headers=headers,
            )
            batch_patient_response = client.post(
                "/crm/patients/lookup/batch",
                json={"patient_ids": [str(ids["patient_a_new"])]},
                headers=headers,
            )

        assert branch_response.status_code == 401
        assert patient_response.status_code == 401
        assert batch_patient_response.status_code == 401
    finally:
        engine.dispose()
