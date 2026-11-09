from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.dependencies.security import get_workspace_access
from app.api.routes import onboarding
from app.models.channel_connection import ChannelConnection
from app.models.clinic_integration import ClinicIntegration
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.schemas.clinic_setup_v2 import (
    ClinicDoctorCreateV2,
    ClinicProfileUpsert,
    ClinicServiceCreateV2,
    WorkingHourInputV2,
    WorkingHoursUpdateV2,
)
from app.schemas.onboarding import WorkspaceCreate
from app.services.clinic_setup_v2 import (
    build_setup_v2_snapshot,
    create_doctor_v2,
    create_service_v2,
    replace_clinic_hours_v2,
    replace_regular_doctor_hours_v2,
    upsert_clinic_profile,
)
from app.services.workspace_runtime_policy import workspace_runtime_policy


def _engine():
    import os

    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database != "ci_db":
        pytest.fail("Workspace onboarding PostgreSQL gate requires disposable local ci_db.")
    return create_engine(url, connect_args={"connect_timeout": 3})


@pytest.fixture
def pg_case():
    engine = _engine()
    conn = engine.connect()
    outer = conn.begin()
    db = Session(bind=conn, join_transaction_mode="create_savepoint")
    user = User(email=f"onboarding-{uuid4()}@example.test", auth_user_id=uuid4())
    db.add(user)
    db.commit()
    try:
        yield db, user
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        conn.close()
        engine.dispose()


def _payload(slug: str, timezone: str = "Africa/Cairo") -> WorkspaceCreate:
    return WorkspaceCreate(name="Clinic Test", slug=slug, timezone=timezone)


@pytest.mark.parametrize("timezone", ["Africa/Cairo", "Asia/Riyadh", "Asia/Dubai"])
def test_timezone_validation_accepts_supported_iana_zones(timezone: str) -> None:
    assert _payload(f"tz-{uuid4().hex[:12]}", timezone).timezone == timezone


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError):
        _payload(f"tz-{uuid4().hex[:12]}", "Mars/Olympus")


def test_first_clinic_creation_persists_timezone_admin_integration_and_real_policy(pg_case) -> None:
    db, user = pg_case
    result = onboarding.create_workspace(_payload(f"first-{uuid4().hex[:12]}", "Asia/Riyadh"), user, db)
    workspace = db.get(Workspace, result.workspace_id)
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == result.workspace_id))
    integration = db.get(ClinicIntegration, result.workspace_id)
    assert workspace is not None
    assert workspace.timezone == "Asia/Riyadh"
    assert workspace.is_demo is False
    assert member is not None and member.user_id == user.id and member.role == "admin" and member.is_active
    assert integration is not None and integration.status == "active" and integration.adapter_key == "tia_database"
    assert db.scalar(select(func.count()).select_from(ChannelConnection).where(ChannelConnection.workspace_id == workspace.id)) == 0


def test_workspace_creation_rolls_back_all_rows_on_integration_failure(pg_case, monkeypatch) -> None:
    db, user = pg_case
    slug = f"rollback-{uuid4().hex[:12]}"
    real_model = ClinicIntegration

    def invalid_integration(**kwargs):
        return real_model(**{**kwargs, "status": "invalid"})

    monkeypatch.setattr(onboarding, "ClinicIntegration", invalid_integration)
    with pytest.raises(HTTPException) as exc:
        onboarding.create_workspace(_payload(slug), user, db)
    assert exc.value.status_code == 409
    assert db.scalar(select(Workspace).where(Workspace.slug == slug)) is None


def test_duplicate_slug_returns_controlled_conflict(pg_case) -> None:
    db, user = pg_case
    slug = f"duplicate-{uuid4().hex[:12]}"
    onboarding.create_workspace(_payload(slug), user, db)
    with pytest.raises(HTTPException) as exc:
        onboarding.create_workspace(_payload(slug), user, db)
    assert exc.value.status_code == 409


def test_concurrent_workspace_creation_same_slug_has_one_success_and_one_conflict() -> None:
    engine = _engine()
    slug = f"race-{uuid4().hex[:12]}"
    user_id = uuid4()
    auth_id = uuid4()
    barrier = Barrier(2)
    with Session(engine) as setup:
        setup.add(User(id=user_id, email=f"race-{uuid4()}@example.test", auth_user_id=auth_id))
        setup.commit()

    def create_once():
        with Session(engine) as db:
            user = db.get(User, user_id)
            barrier.wait()
            try:
                return ("ok", onboarding.create_workspace(_payload(slug), user, db).workspace_id)
            except HTTPException as exc:
                return ("conflict", exc.status_code)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _n: create_once(), range(2)))
        assert sorted(kind for kind, _value in results) == ["conflict", "ok"]
        assert next(value for kind, value in results if kind == "conflict") == 409
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(Workspace).where(Workspace.slug == slug)) == 1
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(Workspace).where(Workspace.slug == slug))
            cleanup.execute(delete(User).where(User.id == user_id))
            cleanup.commit()
        engine.dispose()


def test_same_user_can_create_second_clinic_with_admin_membership(pg_case) -> None:
    db, user = pg_case
    first = onboarding.create_workspace(_payload(f"multi-a-{uuid4().hex[:10]}"), user, db)
    second = onboarding.create_workspace(_payload(f"multi-b-{uuid4().hex[:10]}"), user, db)
    memberships = list(db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)))
    assert {row.workspace_id for row in memberships} >= {first.workspace_id, second.workspace_id}
    assert all(row.role == "admin" for row in memberships if row.workspace_id in {first.workspace_id, second.workspace_id})


def test_cross_tenant_workspace_access_is_denied(pg_case) -> None:
    db, user_a = pg_case
    user_b = User(email=f"tenant-b-{uuid4()}@example.test", auth_user_id=uuid4())
    db.add(user_b)
    db.commit()
    workspace_b = onboarding.create_workspace(_payload(f"tenant-b-{uuid4().hex[:10]}"), user_b, db)
    with pytest.raises(HTTPException) as exc:
        get_workspace_access(x_workspace_id=workspace_b.workspace_id, user=user_a, db=db)
    assert exc.value.status_code == 403


def test_setup_correct_workspace_and_booking_readiness(pg_case) -> None:
    db, user = pg_case
    created = onboarding.create_workspace(_payload(f"setup-{uuid4().hex[:10]}", "Asia/Dubai"), user, db)
    workspace = db.get(Workspace, created.workspace_id)
    upsert_clinic_profile(db, workspace=workspace, payload=ClinicProfileUpsert(name="Clinic Dubai", city="Dubai"))
    service = create_service_v2(db, workspace=workspace, payload=ClinicServiceCreateV2(name="Consultation", duration_minutes=30, price="500"))
    doctor = create_doctor_v2(
        db,
        workspace=workspace,
        payload=ClinicDoctorCreateV2(full_name="Dr Test", service_ids=[service.id]),
    )
    hours = WorkingHoursUpdateV2(
        intervals=[WorkingHourInputV2(weekday=0, start_time="09:00", end_time="17:00")]
    )
    replace_clinic_hours_v2(db, workspace=workspace, payload=hours)
    replace_regular_doctor_hours_v2(db, workspace=workspace, doctor_id=doctor.id, payload=hours)
    snapshot = build_setup_v2_snapshot(db, workspace=workspace)
    assert snapshot.workspace_id == created.workspace_id
    assert snapshot.clinic.timezone == "Asia/Dubai"
    assert workspace.timezone == "Asia/Dubai"
    assert snapshot.readiness.ready is True


def test_incomplete_workspace_is_not_booking_ready_and_has_no_live_channel(pg_case) -> None:
    db, user = pg_case
    created = onboarding.create_workspace(_payload(f"not-ready-{uuid4().hex[:10]}"), user, db)
    workspace = db.get(Workspace, created.workspace_id)
    snapshot = build_setup_v2_snapshot(db, workspace=workspace)
    assert snapshot.readiness.ready is False
    assert db.scalar(select(func.count()).select_from(ChannelConnection).where(ChannelConnection.workspace_id == workspace.id)) == 0


def test_demo_runtime_policy_regression() -> None:
    demo = Workspace(name="Demo", slug=f"demo-{uuid4().hex[:8]}", is_demo=True)
    real = Workspace(name="Real", slug=f"real-{uuid4().hex[:8]}", is_demo=False)
    assert workspace_runtime_policy(demo).allow_external_dispatch is False
    assert workspace_runtime_policy(real).allow_external_dispatch is True


def test_post_create_workspace_selection_and_setup_redirect_contract() -> None:
    source = Path("../frontend/src/app/onboarding/actions.ts").read_text(encoding="utf-8")
    assert 'store.set("tia_workspace_id",created.workspace_id' in source
    assert 'redirect("/setup")' in source


def test_stale_workspace_cookie_falls_back_to_valid_membership_contract() -> None:
    helper = Path("../frontend/src/lib/tia/workspace.ts").read_text(encoding="utf-8")
    auth_route = Path("app/api/routes/auth.py").read_text(encoding="utf-8")
    assert 'if (!me.workspaces.length) redirect("/onboarding")' in helper
    assert "me.workspaces.find((item) => item.workspace_id === selectedId)" in helper
    assert "me.workspaces[0].workspace_id" in helper
    assert "WorkspaceMember.is_active.is_(True)" in auth_route
    assert "if membership.workspace.is_active" in auth_route


def test_add_clinic_cta_reuses_onboarding_flow() -> None:
    shell = Path("../frontend/src/components/dashboard-shell.tsx").read_text(encoding="utf-8")
    assert shell.count('href="/onboarding"') >= 2
    assert "إضافة عيادة" in shell


def test_setup_api_is_workspace_scoped_through_access_dependency() -> None:
    source = Path("app/api/routes/clinic_setup_v2.py").read_text(encoding="utf-8")
    assert 'Depends(require_page_permission("setup", legacy_access="admin"))' in source
    assert "WorkspaceAccess" in source
    assert "access.workspace" in source


def test_arabic_name_slug_fallback_remains_internal_and_unique_contract() -> None:
    source = Path("../frontend/src/app/onboarding/actions.ts").read_text(encoding="utf-8")
    assert 'replace(/[^a-z0-9]+/g,"-")' in source
    assert "clinic-${randomUUID().slice(0,8)}" in source
    assert 'formData.get("slug")' in source
