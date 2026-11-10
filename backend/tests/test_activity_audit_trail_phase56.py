from pathlib import Path
from uuid import uuid4

from app.services.activity import ACTIVITY_ALLOWED_DAYS, _safe_activity_metadata
from app.services.operational_readiness import EXPECTED_MIGRATION_HEAD


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_activity_metadata_drops_contact_message_and_secret_fields() -> None:
    value = {
        "status": "confirmed",
        "phone": "+201234",
        "email": "patient@example.com",
        "message_body": "private customer text",
        "token": "secret",
        "target_user_id": uuid4(),
        "nested": {"content": "private", "priority": "high"},
    }
    clean = _safe_activity_metadata(value)
    assert clean["status"] == "confirmed"
    assert "phone" not in clean
    assert "email" not in clean
    assert "message_body" not in clean
    assert "token" not in clean
    assert isinstance(clean["target_user_id"], str)
    assert clean["nested"] == {"priority": "high"}


def test_activity_period_contract_is_bounded_and_uses_integer_query_coercion() -> None:
    assert ACTIVITY_ALLOWED_DAYS == (7, 30, 90)
    route = (_root() / "backend/app/api/routes/operations.py").read_text(encoding="utf-8")
    assert '@router.get("/activity"' in route
    assert "days: int = 7" in route
    assert "days not in ACTIVITY_ALLOWED_DAYS" in route
    assert "Literal[7, 30, 90]" not in route
    assert 'require_section_access("administration")' in route


def test_activity_table_is_append_only_at_api_layer_and_workspace_indexed() -> None:
    model = (_root() / "backend/app/models/activity_event.py").read_text(encoding="utf-8")
    migration = (_root() / "backend/alembic/versions/0028_activity_audit_trail.py").read_text(
        encoding="utf-8"
    )
    operations = (_root() / "backend/app/api/routes/operations.py").read_text(encoding="utf-8")
    assert '__tablename__ = "activity_events"' in model
    assert "TimestampMixin" not in model
    assert "ix_activity_events_workspace_created" in model
    assert 'op.create_table(\n        "activity_events"' in migration
    assert 'revision: str = "0028_activity_audit_trail"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0027_human_handoff_invariant"' in migration
    activity_route = operations.split('@router.get("/activity"', 1)[1]
    assert "@router.post" not in activity_route
    assert "@router.patch" not in activity_route
    assert "@router.delete" not in activity_route


def test_high_impact_mutations_write_activity_in_the_same_transaction() -> None:
    root = _root()
    appointment_ops = (root / "backend/app/services/appointment_operations.py").read_text(encoding="utf-8")
    handoffs = (root / "backend/app/services/handoffs.py").read_text(encoding="utf-8")
    tasks = (root / "backend/app/services/crm_tasks.py").read_text(encoding="utf-8")
    automations = (root / "backend/app/services/automations.py").read_text(encoding="utf-8")
    auth = (root / "backend/app/api/routes/auth.py").read_text(encoding="utf-8")
    knowledge = (root / "backend/app/services/agent_knowledge_edit.py").read_text(encoding="utf-8")
    clinic = (root / "backend/app/api/routes/clinic.py").read_text(encoding="utf-8")
    setup_v2 = (root / "backend/app/api/routes/clinic_setup_v2.py").read_text(encoding="utf-8")
    history = (root / "backend/app/services/historical_import.py").read_text(encoding="utf-8")

    for action in (
        "appointment.confirmed",
        "appointment.cancelled",
        "appointment.rescheduled",
        "appointment.{target_status}",
    ):
        assert action in appointment_ops
    for action in (
        "handoff.created",
        "handoff.claimed",
        "handoff.assigned",
        "handoff.staff_replied",
        "handoff.resolved",
    ):
        assert action in handoffs
    assert "crm_task.created" in tasks
    assert "crm_task.claimed" in tasks
    assert "automation.job_retried" in automations
    assert "automation.job_cancelled" in automations
    assert "workspace.member_role_changed" in auth
    assert "workspace.member_removed" in auth
    assert "clinic.knowledge_applied" in knowledge
    for action in (
        "clinic.branch_created",
        "clinic.branch_updated",
        "clinic.doctor_created",
        "clinic.doctor_updated",
        "clinic.service_created",
        "clinic.service_updated",
    ):
        assert action in clinic
    for action in (
        "clinic.profile_updated",
        "clinic.service_created",
        "clinic.service_updated",
        "clinic.doctor_created",
        "clinic.doctor_updated",
        "clinic.hours_updated",
        "clinic.doctor_hours_updated",
        "clinic.doctor_visiting_windows_updated",
        "clinic.booking_policy_updated",
    ):
        assert action in setup_v2
    for action in (
        "clinic.history_import_started",
        "clinic.history_imported",
        "clinic.history_import_failed",
    ):
        assert action in history

    retry = automations.split("def retry_automation_job(", 1)[1].split("def cancel_automation_job(", 1)[0]
    assert retry.index("record_activity_event(") < retry.index("db.commit()")
    knowledge_apply = knowledge.split("def apply_agent_knowledge_edit(", 1)[1]
    assert knowledge_apply.index("record_activity_event(") < knowledge_apply.index("db.commit()")
    branch_create = clinic.split("def create_branch(", 1)[1].split('@router.get("/branches"', 1)[0]
    assert branch_create.index("record_activity_event(") < branch_create.index("commit_or_conflict(")
    profile_save = setup_v2.split("def save_profile_v2(", 1)[1].split('@router.post("/setup-v2/services"', 1)[0]
    assert profile_save.index("_activity(") < profile_save.index("_commit(db)")
    history_complete = history.split('action="clinic.history_imported"', 1)[1][:700]
    assert "db.commit()" in history_complete
    assert 'flush=False' in branch_create


def test_ai_appointment_mutations_are_attributed_to_tia_not_staff() -> None:
    adapter = (_root() / "backend/app/integrations/clinic/tia_database.py").read_text(encoding="utf-8")
    assert 'action="appointment.created"' in adapter
    assert 'actor_type="ai"' in adapter
    for marker in (
        'reason="appointment_confirmed_by_ai"',
        'reason=request.reason.strip() or "appointment_rescheduled_by_ai"',
    ):
        section = adapter.split(marker, 1)[1][:400]
        assert 'actor_type="ai"' in section


def test_activity_ui_uses_administration_section_access_and_is_filterable_on_mobile() -> None:
    root = _root()
    page = (root / "frontend/src/app/(dashboard)/activity/page.tsx").read_text(encoding="utf-8")
    layout = (root / "frontend/src/app/(dashboard)/activity/layout.tsx").read_text(encoding="utf-8")
    nav = (root / "frontend/src/components/dashboard-navigation.tsx").read_text(encoding="utf-8")
    types = (root / "frontend/src/lib/types.ts").read_text(encoding="utf-8")

    assert 'requireSectionAccess("administration")' in layout
    assert '/operations/activity?${query.toString()}' in page
    assert '[["7", "7 أيام"], ["30", "30 يوم"], ["90", "90 يوم"]]' in page
    assert 'actor_type' in page
    assert 'entity_type' in page
    assert "لا يخزن نصوص رسائل العملاء" in page
    assert '"/activity"' in nav and 'سجل النشاط' in nav
    assert '"/team"' in nav and 'الفريق' in nav
    assert "export interface ActivityEvent" in types


def test_readiness_tracks_activity_audit_migration_head() -> None:
    assert EXPECTED_MIGRATION_HEAD == "0072_workspace_demo_policy"
