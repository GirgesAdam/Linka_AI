from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.models.automation_job import AutomationJob
from app.models.crm_task import CRMTask
from app.schemas.automation import AutomationClaimedJob
from app.schemas.crm import CRMTaskCreate


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_ai_followup_migration_extends_tasks_and_reuses_automation_worker() -> None:
    migration = (_root() / "backend/alembic/versions/0024_ai_followup_execution.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | Sequence[str] | None = "0023_crm_followup_tasks"' in migration
    assert '"execution_mode"' in migration
    assert '"job_kind"' in migration
    assert '"crm_task_id"' in migration
    assert "crm_follow_up" in migration
    assert "source = 'ai'" in migration
    assert "crm-followup:" in migration


def test_models_can_represent_ai_followup_job_without_fake_appointment() -> None:
    task_id = uuid4()
    patient_id = uuid4()
    workspace_id = uuid4()
    due = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    task = CRMTask(
        workspace_id=workspace_id,
        patient_id=patient_id,
        task_type="follow_up",
        source="ai",
        execution_mode="ai",
        status="pending",
        priority="normal",
        title="Remind customer",
        due_at=due,
    )
    job = AutomationJob(
        workspace_id=workspace_id,
        rule_id=None,
        appointment_id=None,
        crm_task_id=task_id,
        patient_id=patient_id,
        job_kind="crm_follow_up",
        status="queued",
        scheduled_for=due,
        dedupe_key=f"crm-followup:{task_id}",
        attempts=0,
    )
    assert task.execution_mode == "ai"
    assert job.job_kind == "crm_follow_up"
    assert job.appointment_id is None
    assert job.rule_id is None


def test_manual_api_stays_backward_compatible_but_can_request_ai_execution() -> None:
    due = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    normal = CRMTaskCreate(patient_id=uuid4(), title="Follow up", due_at=due)
    automatic = CRMTaskCreate(
        patient_id=uuid4(),
        title="Follow up",
        due_at=due,
        execution_mode="ai",
    )
    assert normal.execution_mode == "human"
    assert automatic.execution_mode == "ai"


def test_claimed_job_contract_is_backward_compatible_for_n8n_job_id_loop() -> None:
    claimed = AutomationClaimedJob(
        job_id=uuid4(),
        job_kind="crm_follow_up",
        crm_task_id=uuid4(),
        patient_id=uuid4(),
        scheduled_for=datetime(2026, 8, 26, 15, 0, tzinfo=UTC),
        attempt=1,
    )
    assert claimed.job_kind == "crm_follow_up"
    assert claimed.appointment_id is None
    assert claimed.rule_key is None


def test_scheduler_claim_and_execute_support_followups_without_second_worker() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    workflow = (_root() / "n8n/workflows/tia_automation_scheduler.json").read_text(encoding="utf-8")
    assert 'job.job_kind == "crm_follow_up"' in service
    assert "_execute_crm_followup_job" in service
    assert 'job_kind="crm_follow_up"' in (_root() / "backend/app/services/crm_tasks.py").read_text(encoding="utf-8")
    # The existing n8n scheduler expands any claimed row and only needs job_id.
    assert "$json.job_id" in workflow
    assert "/automations/adapter/jobs/" in workflow


def test_followup_composer_is_one_wording_call_with_no_tools_or_routing() -> None:
    composer = (_root() / "backend/app/agents/followup_composer.py").read_text(encoding="utf-8")
    assert "compose_followup_message" in composer
    assert "build_realtime_composer_model" in composer
    assert ".bind_tools(" not in composer
    assert "semantic_router" not in composer
    assert "re.compile" not in composer
    assert "recent_conversation" in composer
    assert "continuation, not an ad" in composer
    assert "natural Egyptian Arabic" in composer


def test_followup_execution_releases_db_lock_before_llm_and_rechecks_context_after() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    commit_pos = service.index("# Release database locks before provider latency")
    composer_pos = service.index("compose_followup_message(", commit_pos)
    final_lock_pos = service.index("final_conversation = db.scalar", composer_pos)
    context_recheck_pos = service.index("conversation_changed_during_generation", final_lock_pos)
    assert commit_pos < composer_pos < final_lock_pos < context_recheck_pos
    assert "conversation_human_owned_or_handoff_active" in service
    assert "follow_up_authority_changed_during_generation" in service
    assert "conversation_outbox_busy" in service


def test_ai_followup_uses_normal_ai_outbox_and_delivery_projects_back_to_task() -> None:
    automation = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    channels = (_root() / "backend/app/services/channels.py").read_text(encoding="utf-8")
    tasks = (_root() / "backend/app/services/crm_tasks.py").read_text(encoding="utf-8")
    ownership = (_root() / "backend/app/services/conversation_ownership.py").read_text(encoding="utf-8")
    assert 'sender_type="ai"' in automation
    assert '"source": "ai_followup"' in automation
    assert '"crm_task_id": str(final_task.id)' in automation
    assert "reconcile_ai_followup_dispatch" in channels
    assert 'dispatch.status in {"sent", "delivered", "read"}' in tasks
    assert 'dispatch.status in {"failed", "cancelled"}' in tasks
    assert "ai_dispatch_is_sendable" in ownership


def test_staff_claim_or_assignment_stops_scheduled_ai_followup() -> None:
    service = (_root() / "backend/app/services/crm_tasks.py").read_text(encoding="utf-8")
    assert 'reason="staff_claim"' in service
    assert 'reason="staff_assignment"' in service
    assert 'task.execution_mode = "human"' in service
    assert 'job.status = "cancelled"' in service


def test_patient_profile_creates_shared_human_followup_without_executor_selector() -> None:
    patient_page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(
        encoding="utf-8"
    )
    patient_actions = (_root() / "frontend/src/app/(dashboard)/patients/actions.ts").read_text(
        encoding="utf-8"
    )
    tasks_page = (_root() / "frontend/src/app/(dashboard)/tasks/page.tsx").read_text(
        encoding="utf-8"
    )

    assert 'name="execution_mode"' not in patient_page
    assert '<option value="ai">' not in patient_page
    assert '<option value="human">' not in patient_page
    assert 'name="assigned_user_id"' not in patient_page
    assert 'assigned_user_id: null' in patient_actions
    assert 'execution_mode: "human"' in patient_actions
    assert "execution_mode: executionMode" not in patient_actions
    # Legacy AI / assigned tasks still render correctly in the shared queue.
    assert 'task.execution_mode === "ai"' in tasks_page
    assert 'task.execution_mode === "human"' in tasks_page


def test_whatsapp_followup_models_customer_service_window_explicitly() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    assert "_latest_patient_inbound_at" in service
    assert "_whatsapp_customer_service_window_open" in service
    assert "timedelta(hours=24)" in service
    assert 'Message.sender_type == "patient"' in service
    assert 'Message.direction == "inbound"' in service


def test_outside_24h_uses_configured_approved_template_without_llm() -> None:
    service = (_root() / "backend/app/services/automations.py").read_text(encoding="utf-8")
    window_pos = service.index("if not _whatsapp_customer_service_window_open(")
    template_pos = service.index("_select_ai_followup_template(", window_pos)
    template_dispatch_pos = service.index("_dispatch_ai_followup_template(", template_pos)
    composer_pos = service.index("compose_followup_message(", template_dispatch_pos)
    assert window_pos < template_pos < template_dispatch_pos < composer_pos
    assert "_ai_followup_template_candidates" in service
    assert "_latest_ai_followup_template_name" in service
    assert 'message_type="template"' in service
    assert '"delivery_mode": "approved_template"' in service
    assert '"whatsapp_template"' in service
    assert 'reason="approved_whatsapp_followup_template_required"' in service


def test_followup_template_is_platform_managed_instead_of_admin_typed() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/automations/page.tsx").read_text(
        encoding="utf-8"
    )
    action = (_root() / "frontend/src/app/(dashboard)/automations/actions.ts").read_text(
        encoding="utf-8"
    )
    onboarding = (
        _root() / "frontend/src/app/(dashboard)/automations/whatsapp-direct-onboarding.tsx"
    ).read_text(encoding="utf-8")
    transport = (_root() / "backend/app/services/meta_whatsapp_transport.py").read_text(
        encoding="utf-8"
    )
    channels_page = (_root() / "frontend/src/app/(dashboard)/channels/page.tsx").read_text(
        encoding="utf-8"
    )

    assert 'name="template_names"' not in page
    assert 'name="template_language"' not in page
    assert "saveAiFollowupTemplates" not in action
    assert "provision_standard_whatsapp_templates" in transport
    assert '"ai_followup_templates"' in transport
    assert "قوالب الرسائل" in onboarding
    assert 'redirect("/automations")' in channels_page


def test_automation_setup_documents_approved_template_fallback() -> None:
    setup = (_root() / "n8n/AUTOMATIONS_SETUP.md").read_text(encoding="utf-8")
    assert "AI CRM follow-ups and the 24-hour WhatsApp window" in setup
    assert "Meta-approved" in setup
    assert "ai_followup_template" in setup
    assert "falls back to a human CRM task" in setup
