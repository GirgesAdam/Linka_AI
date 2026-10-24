from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.models.crm_campaign import CRMCampaign, CRMCampaignRecipient
from app.models.message import Message
from app.models.message_dispatch import MessageDispatch
from app.services.crm_campaigns import (
    confirm_cohort_campaign,
    guard_campaign_dispatch_before_claim,
    prepare_cohort_campaign,
)

NOW = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)


def _schema(engine) -> None:
    ddl = [
        """CREATE TABLE workspaces (id CHAR(32) PRIMARY KEY, name VARCHAR(200), slug VARCHAR(120), timezone VARCHAR(64), primary_branch_id CHAR(32), is_active BOOLEAN, is_demo BOOLEAN DEFAULT 0, created_at DATETIME, updated_at DATETIME)""",
        """CREATE TABLE patients (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), first_name VARCHAR(120), last_name VARCHAR(120), phone VARCHAR(40), phone_normalized VARCHAR(40), customer_id TEXT,
            gender VARCHAR(32), birth_date DATE, preferred_language VARCHAR(10), preferred_branch_id CHAR(32), source VARCHAR(32), source_detail VARCHAR(200),
            status VARCHAR(20), marketing_consent BOOLEAN, marketing_consent_at DATETIME, whatsapp_opt_in BOOLEAN DEFAULT 0,
            whatsapp_opt_in_at DATETIME, whatsapp_opt_in_source VARCHAR(32), source_created_at DATETIME, last_contact_at DATETIME,
            created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE crm_cohorts (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), created_by_user_id CHAR(32), name VARCHAR(160), request_key VARCHAR(64), source VARCHAR(24),
            status VARCHAR(16), analytics_operation VARCHAR(48), question TEXT, plan TEXT, period_label VARCHAR(120), member_count INTEGER,
            created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE crm_cohort_members (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), cohort_id CHAR(32), patient_id CHAR(32), rank INTEGER, snapshot_metrics TEXT,
            created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE channel_connections (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), channel VARCHAR(24), provider VARCHAR(40), display_name VARCHAR(120), status VARCHAR(20),
            external_account_id VARCHAR(255), adapter_token_hash VARCHAR(64), created_by_user_id CHAR(32), config TEXT, created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE channel_identities (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), channel_connection_id CHAR(32), patient_id CHAR(32), external_user_id VARCHAR(255),
            display_name VARCHAR(200), phone VARCHAR(40), metadata TEXT, created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE conversations (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), patient_id CHAR(32), channel VARCHAR(24), status VARCHAR(20), external_conversation_id VARCHAR(255),
            channel_connection_id CHAR(32), assigned_user_id CHAR(32), owner_type VARCHAR(16), unread_count INTEGER, ownership_changed_at DATETIME,
            subject VARCHAR(250), started_at DATETIME, last_message_at DATETIME, closed_at DATETIME,
            agent_processing_token CHAR(32), agent_processing_started_at DATETIME, agent_processing_lease_expires_at DATETIME,
            created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE messages (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), conversation_id CHAR(32), channel_connection_id CHAR(32), sender_type VARCHAR(20), direction VARCHAR(20),
            message_type VARCHAR(32), content TEXT, external_message_id VARCHAR(255), delivery_status VARCHAR(20), in_reply_to_message_id CHAR(32), sent_by_user_id CHAR(32), metadata TEXT,
            created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE message_dispatches (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), channel_connection_id CHAR(32), message_id CHAR(32), status VARCHAR(20), attempts INTEGER,
            provider_message_id VARCHAR(255), last_error VARCHAR(2000), next_attempt_at DATETIME, locked_at DATETIME, sent_at DATETIME, delivered_at DATETIME,
            read_at DATETIME, metadata TEXT, created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE crm_campaigns (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), cohort_id CHAR(32), channel_connection_id CHAR(32), created_by_user_id CHAR(32),
            confirmed_by_user_id CHAR(32), request_key VARCHAR(64), confirmation_key VARCHAR(64), name VARCHAR(160), status VARCHAR(16), template_name VARCHAR(160),
            template_language VARCHAR(32), body_parameter_keys TEXT, rate_limit_per_minute INTEGER, recipient_count INTEGER, eligible_count INTEGER,
            confirmed_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE crm_campaign_recipients (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), campaign_id CHAR(32), patient_id CHAR(32), rank INTEGER, status VARCHAR(32), reason VARCHAR(120),
            conversation_id CHAR(32), channel_identity_id CHAR(32), message_id CHAR(32), dispatch_id CHAR(32), scheduled_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE activity_events (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), actor_type VARCHAR(20), actor_user_id CHAR(32), action VARCHAR(80), entity_type VARCHAR(40),
            entity_id CHAR(32), summary VARCHAR(500), metadata TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _schema(engine)
    return engine


def _seed(db: Session, *, eligible=2, no_consent=1, inactive=1, no_route=1):
    ids = {key: uuid4() for key in ("workspace", "user", "cohort", "connection")}
    w = ids["workspace"].hex
    db.execute(text("INSERT INTO workspaces (id,name,slug,timezone,is_active,created_at,updated_at) VALUES (:id,'Glow Clinic','glow','Africa/Cairo',1,:now,:now)"), {"id": w, "now": NOW.isoformat()})
    db.execute(text("INSERT INTO channel_connections (id,workspace_id,channel,provider,display_name,status,external_account_id,adapter_token_hash,created_by_user_id,config,created_at,updated_at) VALUES (:id,:w,'whatsapp','n8n','Main WhatsApp','active','acct','hash',:u,'{}',:now,:now)"), {"id": ids["connection"].hex, "w": w, "u": ids["user"].hex, "now": NOW.isoformat()})

    patients: list[tuple] = []
    rank = 1
    for kind, count in (("eligible", eligible), ("no_consent", no_consent), ("inactive", inactive), ("no_route", no_route)):
        for _index in range(count):
            pid = uuid4()
            patients.append((pid, kind, rank))
            consent = 0 if kind == "no_consent" else 1
            status = "inactive" if kind == "inactive" else "active"
            db.execute(text("""INSERT INTO patients (id,workspace_id,first_name,last_name,phone,phone_normalized,gender,birth_date,preferred_language,preferred_branch_id,source,source_detail,status,marketing_consent,marketing_consent_at,source_created_at,last_contact_at,created_at,updated_at)
                VALUES (:id,:w,:first,'Ali',:phone,:phone,NULL,NULL,'ar',NULL,'other',NULL,:status,:consent,:consent_at,NULL,NULL,:now,:now)"""), {
                "id": pid.hex, "w": w, "first": f"P{rank}", "phone": f"010000000{rank:02d}", "status": status, "consent": consent,
                "consent_at": NOW.isoformat() if consent else None, "now": NOW.isoformat(),
            })
            if kind != "no_route":
                db.execute(text("INSERT INTO channel_identities (id,workspace_id,channel_connection_id,patient_id,external_user_id,display_name,phone,metadata,created_at,updated_at) VALUES (:id,:w,:c,:p,:ext,:name,NULL,'{}',:now,:now)"), {"id": uuid4().hex, "w": w, "c": ids["connection"].hex, "p": pid.hex, "ext": f"wa-{rank}", "name": f"P{rank}", "now": NOW.isoformat()})
            rank += 1

    db.execute(text("INSERT INTO crm_cohorts (id,workspace_id,created_by_user_id,name,request_key,source,status,analytics_operation,question,plan,period_label,member_count,created_at,updated_at) VALUES (:id,:w,:u,'Lapsed','req','analytics_bi','active','lapsed_patients','q','{}','all time',:count,:now,:now)"), {"id": ids["cohort"].hex, "w": w, "u": ids["user"].hex, "count": len(patients), "now": NOW.isoformat()})
    for pid, _, member_rank in patients:
        db.execute(text("INSERT INTO crm_cohort_members (id,workspace_id,cohort_id,patient_id,rank,snapshot_metrics,created_at,updated_at) VALUES (:id,:w,:c,:p,:r,'[]',:now,:now)"), {"id": uuid4().hex, "w": w, "c": ids["cohort"].hex, "p": pid.hex, "r": member_rank, "now": NOW.isoformat()})
    db.commit()
    ids["patients"] = patients
    return ids


def _prepare(db: Session, ids, request_id=None):
    return prepare_cohort_campaign(
        db,
        workspace_id=ids["workspace"],
        cohort_id=ids["cohort"],
        created_by_user_id=ids["user"],
        request_id=request_id or uuid4(),
        name="Win-back August",
        channel_connection_id=ids["connection"],
        template_name="tia_winback_ar",
        template_language="ar",
        body_parameter_keys=["patient_first_name", "clinic_name"],
        rate_limit_per_minute=20,
    )


def test_campaign_draft_is_preview_only_and_filters_consent_status_route() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db)
        campaign = _prepare(db, ids)
        recipients = list(db.scalars(select(CRMCampaignRecipient).where(CRMCampaignRecipient.campaign_id == campaign.id).order_by(CRMCampaignRecipient.rank)).all())
        assert campaign.recipient_count == 5
        assert campaign.eligible_count == 2
        assert [row.status for row in recipients] == ["eligible", "eligible", "skipped_no_consent", "skipped_inactive", "skipped_no_route"]
        assert db.scalar(select(func.count(Message.id))) == 0
        assert db.scalar(select(func.count(MessageDispatch.id))) == 0


def test_campaign_draft_is_idempotent_per_request() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db, eligible=1, no_consent=0, inactive=0, no_route=0)
        request_id = uuid4()
        first = _prepare(db, ids, request_id=request_id)
        second = _prepare(db, ids, request_id=request_id)
        assert first.id == second.id
        assert db.scalar(select(func.count(CRMCampaign.id))) == 1


def test_confirmation_queues_only_preview_eligible_with_fixed_template_metadata_and_rate_limit() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db, eligible=2, no_consent=1, inactive=0, no_route=0)
        campaign = _prepare(db, ids)
        result = confirm_cohort_campaign(db, workspace_id=ids["workspace"], campaign_id=campaign.id, confirmation_id=uuid4(), actor_user_id=ids["user"], now=NOW)
        assert result["queued_count"] == 2
        assert result["preview_eligible_count"] == 2
        messages = list(db.scalars(select(Message).order_by(Message.created_at, Message.id)).all())
        dispatches = list(db.scalars(select(MessageDispatch).order_by(MessageDispatch.next_attempt_at, MessageDispatch.id)).all())
        assert len(messages) == len(dispatches) == 2
        assert all(m.message_type == "template" and m.sender_type == "staff" for m in messages)
        assert messages[0].metadata_json["source"] == "crm_cohort_campaign"
        assert messages[0].metadata_json["whatsapp_template"]["name"] == "tia_winback_ar"
        assert messages[0].metadata_json["whatsapp_template"]["body_parameters"][1] == "Glow Clinic"
        assert dispatches[1].next_attempt_at - dispatches[0].next_attempt_at >= timedelta(seconds=3)


def test_confirmation_is_idempotent_and_does_not_duplicate_dispatches() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db, eligible=1, no_consent=0, inactive=0, no_route=0)
        campaign = _prepare(db, ids)
        confirmation_id = uuid4()
        first = confirm_cohort_campaign(db, workspace_id=ids["workspace"], campaign_id=campaign.id, confirmation_id=confirmation_id, actor_user_id=ids["user"], now=NOW)
        second = confirm_cohort_campaign(db, workspace_id=ids["workspace"], campaign_id=campaign.id, confirmation_id=uuid4(), actor_user_id=ids["user"], now=NOW)
        assert first["confirmation_id"] == second["confirmation_id"] == confirmation_id
        assert db.scalar(select(func.count(MessageDispatch.id))) == 1


def test_consent_is_rechecked_at_confirmation_and_removed_recipient_is_cancelled() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db, eligible=2, no_consent=0, inactive=0, no_route=0)
        campaign = _prepare(db, ids)
        first_patient = ids["patients"][0][0]
        db.execute(text("UPDATE patients SET marketing_consent=0, marketing_consent_at=NULL WHERE id=:id"), {"id": first_patient.hex})
        db.commit()
        result = confirm_cohort_campaign(db, workspace_id=ids["workspace"], campaign_id=campaign.id, confirmation_id=uuid4(), actor_user_id=ids["user"], now=NOW)
        assert result["queued_count"] == 1
        assert result["cancelled_before_queue"] == 1
        cancelled = db.scalar(select(CRMCampaignRecipient).where(CRMCampaignRecipient.patient_id == first_patient))
        assert cancelled.status == "cancelled_no_consent"


def test_claim_time_guard_cancels_if_consent_withdrawn_after_confirmation() -> None:
    engine = _db()
    with Session(engine) as db:
        ids = _seed(db, eligible=1, no_consent=0, inactive=0, no_route=0)
        campaign = _prepare(db, ids)
        confirm_cohort_campaign(db, workspace_id=ids["workspace"], campaign_id=campaign.id, confirmation_id=uuid4(), actor_user_id=ids["user"], now=datetime.now(UTC) - timedelta(minutes=2))
        patient_id = ids["patients"][0][0]
        db.execute(text("UPDATE patients SET marketing_consent=0, marketing_consent_at=NULL WHERE id=:id"), {"id": patient_id.hex})
        db.commit()
        recipient = db.scalar(select(CRMCampaignRecipient).where(CRMCampaignRecipient.campaign_id == campaign.id))
        dispatch = db.get(MessageDispatch, recipient.dispatch_id)
        message = db.get(Message, recipient.message_id)
        from app.models.conversation import Conversation
        conversation = db.get(Conversation, recipient.conversation_id)
        assert guard_campaign_dispatch_before_claim(db, dispatch=dispatch, message=message, conversation=conversation) is False
        db.commit()
        db.refresh(recipient)
        db.refresh(dispatch)
        assert recipient.status == "cancelled_no_consent"
        assert dispatch.status == "cancelled"


def test_campaign_service_has_no_llm_or_freeform_message_generation() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/services/crm_campaigns.py").read_text(encoding="utf-8")
    assert "generateContent" not in source
    assert "google.genai" not in source
    assert "compose_followup_message" not in source
    assert 'message_type="template"' in source
    assert 'source": "crm_cohort_campaign"' in source
    channels = (root / "app/services/channels.py").read_text(encoding="utf-8")
    assert "guard_campaign_dispatch_before_claim" in channels
    assert "reconcile_campaign_dispatch" in channels


def test_campaign_migration_and_readiness_head() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (root / "alembic/versions/0039_crm_campaigns.py").read_text(encoding="utf-8")
    readiness = (root / "app/services/operational_readiness.py").read_text(encoding="utf-8")
    assert 'revision: str = "0039_crm_campaigns"' in migration
    assert 'down_revision: str | Sequence[str] | None = "0038_crm_cohorts"' in migration
    assert len("0039_crm_campaigns") <= 32
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness


def test_campaign_write_routes_require_admin_and_have_explicit_prepare_confirm_steps() -> None:
    root = Path(__file__).resolve().parents[1]
    route = (root / "app/api/routes/crm.py").read_text(encoding="utf-8")
    assert '"/cohorts/{cohort_id}/campaigns"' in route
    assert '"/campaigns/{campaign_id}/confirm"' in route
    campaign_sections = route[route.index("def prepare_campaign_for_cohort"):]
    assert "Depends(get_workspace_admin)" in campaign_sections


def test_customer_opt_out_is_semantic_tool_not_keyword_router() -> None:
    root = Path(__file__).resolve().parents[1]
    router = (root / "app/agents/turn_models.py").read_text(encoding="utf-8")
    policy = (root / "app/agents/capability_policy.py").read_text(encoding="utf-8")
    tools = (root / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")
    prompt = (root / "app/agents/prompts/customer_service.py").read_text(encoding="utf-8")
    assert '"marketing_preferences"' in router
    assert '"marketing_preferences": frozenset({"update_marketing_consent"})' in policy
    assert "def update_marketing_consent(consent: bool)" in tools
    assert "طلب صريح" in prompt
    assert "re.compile" not in router
    assert "re.search" not in router


def test_frontend_campaign_flow_has_preview_then_separate_confirmation() -> None:
    root = Path(__file__).resolve().parents[2]
    ui = (root / "frontend/src/app/(dashboard)/analytics/cohorts/[cohortId]/campaign-form.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/analytics/actions.ts").read_text(encoding="utf-8")

    assert "prepareState.campaign" in ui
    assert "confirmState.result" in ui
    assert "<form action={prepareAction}" in ui
    assert "<form action={confirmAction}" in ui
    assert "campaign.eligible_count" in ui
    assert "prepareCohortCampaignAction" in actions
    assert "confirmCohortCampaignAction" in actions
    assert "/confirm" in actions