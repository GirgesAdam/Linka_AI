from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.crm_task import CRMTask
from app.schemas.crm import CRMTaskCreate, PatientTimelineEvent, PatientTimelineTask
from app.services.crm_tasks import CRMTaskPermissionError, update_crm_task


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


class _FlushOnlyDB:
    def __init__(self) -> None:
        self.flushes = 0
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self, *args) -> None:
        self.flushes += 1


def _task(*, assigned_user_id=None, status: str = "pending") -> CRMTask:
    now = datetime.now(UTC)
    return CRMTask(
        id=uuid4(),
        workspace_id=uuid4(),
        patient_id=uuid4(),
        lead_id=None,
        conversation_id=None,
        assigned_user_id=assigned_user_id,
        created_by_user_id=None,
        completed_by_user_id=None,
        task_type="follow_up",
        source="manual",
        status=status,
        priority="normal",
        title="Call customer",
        description=None,
        due_at=now + timedelta(hours=2),
        completed_at=None,
        dedupe_key=None,
    )


def test_task_schema_requires_real_patient_due_time_and_normalizes_title() -> None:
    due = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)
    payload = CRMTaskCreate(
        patient_id=uuid4(),
        title="  Follow up after consultation  ",
        due_at=due,
    )
    assert payload.title == "Follow up after consultation"
    assert payload.task_type == "follow_up"
    assert payload.priority == "normal"


def test_member_cannot_update_someone_elses_task() -> None:
    assignee = uuid4()
    actor = uuid4()
    with pytest.raises(CRMTaskPermissionError):
        update_crm_task(
            _FlushOnlyDB(),  # type: ignore[arg-type]
            task=_task(assigned_user_id=assignee),
            actor_user_id=actor,
            actor_is_admin=False,
            updates={"status": "completed"},
            commit=False,
        )


def test_assignee_can_complete_task_and_completion_is_auditable() -> None:
    actor = uuid4()
    db = _FlushOnlyDB()
    task = _task(assigned_user_id=actor)
    updated = update_crm_task(
        db,  # type: ignore[arg-type]
        task=task,
        actor_user_id=actor,
        actor_is_admin=False,
        updates={"status": "completed"},
        commit=False,
    )
    assert updated.status == "completed"
    assert updated.completed_by_user_id == actor
    assert updated.completed_at is not None
    assert db.flushes == 2
    assert any(getattr(item, "action", None) == "crm_task.completed" for item in db.added)


def test_patient_timeline_task_contract_can_represent_completion() -> None:
    now = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)
    task_id = uuid4()
    event = PatientTimelineEvent(
        id=f"task:{task_id}:completed",
        kind="task",
        occurred_at=now,
        actor_type="staff",
        task=PatientTimelineTask(
            id=task_id,
            event_type="completed",
            status="completed",
            priority="high",
            task_type="follow_up",
            title="Call customer",
            due_at=now - timedelta(hours=1),
            assigned_user_id=uuid4(),
        ),
    )
    assert event.task and event.task.event_type == "completed"


def test_task_migration_backfills_legacy_lead_followups_and_indexes_queue() -> None:
    migration = (_root() / "backend/alembic/versions/0023_crm_followup_tasks.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | Sequence[str] | None = "0022_handoff_intelligence"' in migration
    assert '"crm_tasks"' in migration
    assert '"ix_crm_tasks_workspace_queue"' in migration
    assert "SELECT id, workspace_id, patient_id, assigned_user_id, next_follow_up_at" in migration
    assert '"dedupe_key": f"lead-backfill:{row[\'id\']}"' in migration


def test_task_creation_dedupe_is_savepoint_scoped_and_legacy_due_time_is_localized() -> None:
    service = (_root() / "backend/app/services/crm_tasks.py").read_text(encoding="utf-8")
    route = (_root() / "backend/app/api/routes/crm.py").read_text(encoding="utf-8")

    assert "savepoint = db.begin_nested()" in service
    assert "db.flush([task])" in service
    assert "savepoint.rollback()" in service
    assert "db.rollback()\n        if dedupe_key" not in service
    assert "def _workspace_local_datetime(" in route
    assert "value.replace(tzinfo=_workspace_timezone(timezone_name))" in route


def test_task_routes_are_workspace_scoped_and_overdue_is_computed_at_read_time() -> None:
    route = (_root() / "backend/app/api/routes/crm.py").read_text(encoding="utf-8")
    assert '@router.post("/tasks"' in route
    assert '@router.get("/tasks"' in route
    assert '@router.patch("/tasks/{task_id}"' in route
    assert '@router.post("/tasks/{task_id}/claim"' in route
    assert "CRMTask.workspace_id == access.workspace.id" in route
    assert 'Literal["all", "overdue", "today", "upcoming", "due"]' in route
    assert "task.due_at < now" in route
    assert ".with_for_update(of=CRMTask)" in route
    assert "llm" not in route.lower()


def test_ai_followup_uses_existing_semantic_turn_and_backend_validated_tool() -> None:
    semantic = (_root() / "backend/app/agents/turn_models.py").read_text(encoding="utf-8")
    policy = (_root() / "backend/app/agents/capability_policy.py").read_text(encoding="utf-8")
    tools = (_root() / "backend/app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")
    prompt = (_root() / "backend/app/agents/prompts/customer_service.py").read_text(encoding="utf-8")

    assert '"follow_up_request"' in semantic
    assert '"follow_up_request": frozenset({"create_follow_up_task"})' in policy
    assert '"create_follow_up_task": "follow_up_request"' in policy
    assert "def create_follow_up_task(" in tools
    assert "create_crm_task(" in tools
    assert 'dedupe_key=f"agent:{ctx.run_id}:follow_up"' in tools
    assert "Follow-up time must be in the future." in tools
    assert "create_follow_up_task" not in prompt



def test_followup_ui_uses_backend_task_api_and_patient_profile_entrypoint() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/tasks/page.tsx").read_text(encoding="utf-8")
    actions = (_root() / "frontend/src/app/(dashboard)/tasks/actions.ts").read_text(encoding="utf-8")
    patient_page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(
        encoding="utf-8"
    )
    patient_actions = (_root() / "frontend/src/app/(dashboard)/patients/actions.ts").read_text(
        encoding="utf-8"
    )
    nav = (_root() / "frontend/src/components/dashboard-navigation.tsx").read_text(encoding="utf-8")

    assert 'scope: "100"' not in page
    assert 'scope: filters.scope || "all"' in page
    assert "assigned_to_me" in page
    assert "claimTask" in page
    assert "setTaskStatus" in page
    assert "/crm/tasks/${taskId}/claim" in actions
    assert "createPatientTask" in patient_page
    assert 'type="datetime-local"' in patient_page
    assert 'tiaRequest("/crm/tasks"' in patient_actions
    assert 'href: "/tasks"' in nav
    assert "icon: ListTodo" in nav
