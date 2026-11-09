from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.crm_campaign_conversion import CRMCampaignConversion
from app.services.campaign_analytics import campaign_analytics_overview
from app.services.campaign_attribution import (
    record_direct_campaign_booking_conversion,
    transfer_campaign_booking_conversion,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ddl = [
        """CREATE TABLE crm_campaigns (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), cohort_id CHAR(32), channel_connection_id CHAR(32),
            created_by_user_id CHAR(32), confirmed_by_user_id CHAR(32), request_key VARCHAR(64), confirmation_key VARCHAR(64),
            name VARCHAR(160), status VARCHAR(16), template_name VARCHAR(160), template_language VARCHAR(32),
            body_parameter_keys TEXT, rate_limit_per_minute INTEGER, recipient_count INTEGER, eligible_count INTEGER,
            confirmed_at DATETIME, created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE crm_campaign_recipients (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), campaign_id CHAR(32), patient_id CHAR(32), rank INTEGER,
            status VARCHAR(32), reason VARCHAR(120), conversation_id CHAR(32), channel_identity_id CHAR(32), message_id CHAR(32),
            dispatch_id CHAR(32), scheduled_at DATETIME, created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE message_dispatches (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), channel_connection_id CHAR(32), message_id CHAR(32), status VARCHAR(20),
            attempts INTEGER, provider_message_id VARCHAR(255), last_error VARCHAR(2000), next_attempt_at DATETIME, locked_at DATETIME,
            sent_at DATETIME, delivered_at DATETIME, read_at DATETIME, metadata TEXT, created_at DATETIME, updated_at DATETIME
        )""",
        """CREATE TABLE messages (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), conversation_id CHAR(32), direction VARCHAR(20), created_at DATETIME
        )""",
        """CREATE TABLE appointments (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), patient_id CHAR(32), status VARCHAR(20), created_at DATETIME
        )""",
        """CREATE TABLE crm_campaign_conversions (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), campaign_id CHAR(32), recipient_id CHAR(32), patient_id CHAR(32),
            conversation_id CHAR(32), original_appointment_id CHAR(32), appointment_id CHAR(32), response_message_id CHAR(32),
            attribution_kind VARCHAR(48), campaign_sent_at DATETIME, patient_replied_at DATETIME, booked_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE payment_transactions (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), transaction_type VARCHAR(16), currency VARCHAR(3)
        )""",
        """CREATE TABLE payment_allocations (
            id CHAR(32) PRIMARY KEY, workspace_id CHAR(32), transaction_id CHAR(32), appointment_id CHAR(32), amount_minor INTEGER
        )""",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)
    return engine


def _seed_campaign(db: Session, *, second_dispatch: bool = False):
    ids = {name: uuid4() for name in ("workspace", "campaign", "cohort", "connection", "patient", "conversation", "recipient", "dispatch", "message", "appointment")}
    params = {k: v.hex for k, v in ids.items()}
    params.update({"now": NOW.replace(tzinfo=None), "sent": (NOW - timedelta(hours=2)).replace(tzinfo=None)})
    db.execute(text("""INSERT INTO crm_campaigns
        (id,workspace_id,cohort_id,channel_connection_id,created_by_user_id,confirmed_by_user_id,request_key,confirmation_key,name,status,template_name,template_language,body_parameter_keys,rate_limit_per_minute,recipient_count,eligible_count,confirmed_at,created_at,updated_at)
        VALUES (:campaign,:workspace,:cohort,:connection,NULL,NULL,'req','confirm','عودة العملاء','confirmed','winback_ar','ar','[]',10,:recipient_count,:recipient_count,:sent,:sent,:sent)"""),
        {**params, "recipient_count": 2 if second_dispatch else 1})
    db.execute(text("""INSERT INTO message_dispatches
        (id,workspace_id,channel_connection_id,message_id,status,attempts,provider_message_id,last_error,next_attempt_at,locked_at,sent_at,delivered_at,read_at,metadata,created_at,updated_at)
        VALUES (:dispatch,:workspace,:connection,:message,'read',1,'provider-1',NULL,NULL,NULL,:sent,:sent,:sent,'{}',:sent,:sent)"""), params)
    db.execute(text("""INSERT INTO crm_campaign_recipients
        (id,workspace_id,campaign_id,patient_id,rank,status,reason,conversation_id,channel_identity_id,message_id,dispatch_id,scheduled_at,created_at,updated_at)
        VALUES (:recipient,:workspace,:campaign,:patient,1,'read',NULL,:conversation,NULL,:message,:dispatch,:sent,:sent,:sent)"""), params)
    if second_dispatch:
        ids["patient2"], ids["recipient2"], ids["dispatch2"], ids["message2"], ids["conversation2"] = (uuid4() for _ in range(5))
        p2 = {k: v.hex for k, v in ids.items() if isinstance(v, UUID)}
        p2.update({"now": NOW.replace(tzinfo=None), "sent": (NOW - timedelta(hours=1)).replace(tzinfo=None)})
        db.execute(text("""INSERT INTO message_dispatches
            (id,workspace_id,channel_connection_id,message_id,status,attempts,provider_message_id,last_error,next_attempt_at,locked_at,sent_at,delivered_at,read_at,metadata,created_at,updated_at)
            VALUES (:dispatch2,:workspace,:connection,:message2,'failed',1,'provider-2','provider failed',NULL,NULL,:sent,NULL,NULL,'{}',:sent,:sent)"""), p2)
        db.execute(text("""INSERT INTO crm_campaign_recipients
            (id,workspace_id,campaign_id,patient_id,rank,status,reason,conversation_id,channel_identity_id,message_id,dispatch_id,scheduled_at,created_at,updated_at)
            VALUES (:recipient2,:workspace,:campaign,:patient2,2,'failed','provider failed',:conversation2,NULL,:message2,:dispatch2,:sent,:sent,:sent)"""), p2)
    db.commit()
    return ids


def test_direct_booking_attribution_requires_real_same_conversation_response() -> None:
    engine = _engine()
    with Session(engine) as db:
        ids = _seed_campaign(db)
        sent_at = NOW - timedelta(hours=2)
        db.execute(text("INSERT INTO appointments (id,workspace_id,patient_id,status,created_at) VALUES (:id,:w,:p,'confirmed',:now)"), {"id": ids["appointment"].hex, "w": ids["workspace"].hex, "p": ids["patient"].hex, "now": NOW.replace(tzinfo=None)})
        # A response before the campaign send must not create attribution.
        db.execute(text("INSERT INTO messages (id,workspace_id,conversation_id,direction,created_at) VALUES (:id,:w,:c,'inbound',:at)"), {"id": uuid4().hex, "w": ids["workspace"].hex, "c": ids["conversation"].hex, "at": (sent_at - timedelta(minutes=1)).replace(tzinfo=None)})
        db.commit()
        assert record_direct_campaign_booking_conversion(db, workspace_id=ids["workspace"], patient_id=ids["patient"], conversation_id=ids["conversation"], appointment_id=ids["appointment"], booked_at=NOW) is None

        response_id = uuid4()
        db.execute(text("INSERT INTO messages (id,workspace_id,conversation_id,direction,created_at) VALUES (:id,:w,:c,'inbound',:at)"), {"id": response_id.hex, "w": ids["workspace"].hex, "c": ids["conversation"].hex, "at": (sent_at + timedelta(minutes=10)).replace(tzinfo=None)})
        db.commit()
        conversion = record_direct_campaign_booking_conversion(db, workspace_id=ids["workspace"], patient_id=ids["patient"], conversation_id=ids["conversation"], appointment_id=ids["appointment"], booked_at=NOW)
        assert conversion is not None
        assert conversion.campaign_id == ids["campaign"]
        assert conversion.response_message_id == response_id
        assert conversion.attribution_kind == "direct_same_conversation_response"
        # Idempotent for the same appointment.
        assert record_direct_campaign_booking_conversion(db, workspace_id=ids["workspace"], patient_id=ids["patient"], conversation_id=ids["conversation"], appointment_id=ids["appointment"], booked_at=NOW).id == conversion.id


def test_tracked_conversion_follows_replacement_appointment_on_reschedule() -> None:
    engine = _engine()
    with Session(engine) as db:
        ids = _seed_campaign(db)
        replacement = uuid4()
        response = uuid4()
        for appointment_id in (ids["appointment"], replacement):
            db.execute(text("INSERT INTO appointments (id,workspace_id,patient_id,status,created_at) VALUES (:id,:w,:p,'confirmed',:now)"), {"id": appointment_id.hex, "w": ids["workspace"].hex, "p": ids["patient"].hex, "now": NOW.replace(tzinfo=None)})
        db.execute(text("INSERT INTO messages (id,workspace_id,conversation_id,direction,created_at) VALUES (:id,:w,:c,'inbound',:at)"), {"id": response.hex, "w": ids["workspace"].hex, "c": ids["conversation"].hex, "at": (NOW - timedelta(hours=1)).replace(tzinfo=None)})
        db.commit()
        conversion = record_direct_campaign_booking_conversion(db, workspace_id=ids["workspace"], patient_id=ids["patient"], conversation_id=ids["conversation"], appointment_id=ids["appointment"], booked_at=NOW)
        assert conversion is not None
        transfer_campaign_booking_conversion(db, workspace_id=ids["workspace"], from_appointment_id=ids["appointment"], to_appointment_id=replacement)
        db.commit()
        row = db.scalar(select(CRMCampaignConversion))
        assert row.original_appointment_id == ids["appointment"]
        assert row.appointment_id == replacement


def test_campaign_analytics_uses_provider_delivery_facts_and_allocated_net_revenue_only() -> None:
    engine = _engine()
    with Session(engine) as db:
        ids = _seed_campaign(db, second_dispatch=True)
        response = uuid4()
        db.execute(text("INSERT INTO appointments (id,workspace_id,patient_id,status,created_at) VALUES (:id,:w,:p,'completed',:now)"), {"id": ids["appointment"].hex, "w": ids["workspace"].hex, "p": ids["patient"].hex, "now": NOW.replace(tzinfo=None)})
        db.execute(text("INSERT INTO messages (id,workspace_id,conversation_id,direction,created_at) VALUES (:id,:w,:c,'inbound',:at)"), {"id": response.hex, "w": ids["workspace"].hex, "c": ids["conversation"].hex, "at": (NOW - timedelta(hours=1)).replace(tzinfo=None)})
        db.commit()
        assert record_direct_campaign_booking_conversion(db, workspace_id=ids["workspace"], patient_id=ids["patient"], conversation_id=ids["conversation"], appointment_id=ids["appointment"], booked_at=NOW)
        payment, refund, usd = uuid4(), uuid4(), uuid4()
        for tid, kind, amount, currency in ((payment, "payment", 200_000, "EGP"), (refund, "refund", 50_000, "EGP"), (usd, "payment", 999_999, "USD")):
            db.execute(text("INSERT INTO payment_transactions (id,workspace_id,transaction_type,currency) VALUES (:id,:w,:kind,:currency)"), {"id": tid.hex, "w": ids["workspace"].hex, "kind": kind, "currency": currency})
            db.execute(text("INSERT INTO payment_allocations (id,workspace_id,transaction_id,appointment_id,amount_minor) VALUES (:id,:w,:t,:a,:amount)"), {"id": uuid4().hex, "w": ids["workspace"].hex, "t": tid.hex, "a": ids["appointment"].hex, "amount": amount})
        db.commit()

        result = campaign_analytics_overview(db, workspace_id=ids["workspace"], days=90, now=NOW)
        assert len(result.campaigns) == 1
        row = result.campaigns[0]
        assert row.sent_count == 2
        assert row.delivered_count == 1
        assert row.read_count == 1
        assert row.failed_count == 1
        assert row.delivery_rate == 50.0
        assert row.read_rate == 100.0
        assert row.tracked_booking_count == 1
        assert row.completed_booking_count == 1
        assert row.booking_conversion_rate == 50.0
        assert row.attributed_revenue_minor == 150_000
        assert result.historical_booking_backfill is False


def test_phase76_routes_migration_and_frontend_are_explicit_about_attribution() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = (root / "alembic/versions/0044_campaign_analytics_tracking.py").read_text(encoding="utf-8")
    readiness = (root / "app/services/operational_readiness.py").read_text(encoding="utf-8")
    route = (root / "app/api/routes/campaign_analytics.py").read_text(encoding="utf-8")
    attribution = (root / "app/services/campaign_attribution.py").read_text(encoding="utf-8")
    tools = (root / "app/agents/tools/clinic_tools.py").read_text(encoding="utf-8")
    operations = (root / "app/services/appointment_operations.py").read_text(encoding="utf-8")
    frontend = root.parent / "frontend/src/app/(dashboard)/analytics/campaigns/page.tsx"
    frontend_detail = root.parent / "frontend/src/app/(dashboard)/analytics/campaigns/[campaignId]/page.tsx"
    analytics_page = root.parent / "frontend/src/app/(dashboard)/analytics/page.tsx"
    assert 'revision: str = "0044_campaign_analytics_tracking"' in migration
    assert 'down_revision: str | None = "0043_analytics_scale_guards"' in migration
    assert 'REVOKE ALL ON TABLE public."crm_campaign_conversions" FROM anon, authenticated' in migration
    assert 'EXPECTED_MIGRATION_HEAD = "0072_workspace_demo_policy"' in readiness
    assert '@router.get("/campaigns"' in route and '@router.get("/campaigns/{campaign_id}"' in route
    assert "get_analytics_db" in route and "get_analytics_page_reader" in route
    assert 'Depends(get_analytics_page_reader("analytics.campaigns", legacy_access="reader"))' in route
    assert 'Depends(get_analytics_page_reader("analytics.campaign_detail", legacy_access="reader"))' in route
    assert "direct_same_conversation_response" in attribution
    assert "MessageDispatch.sent_at" in attribution
    assert "Message.direction == \"inbound\"" in attribution
    assert "record_direct_campaign_booking_conversion" in tools
    assert "transfer_campaign_booking_conversion" in operations
    assert frontend.exists() and frontend_detail.exists()
    assert "حجوزات متتبعة" in frontend.read_text(encoding="utf-8")
    detail_source = frontend_detail.read_text(encoding="utf-8")
    assert "campaign.booking_conversion_rate" in detail_source
    assert "campaign.attributed_revenue_minor" in detail_source
    assert "data.definitions.map" in detail_source
    assert "أداء الحملات" in analytics_page.read_text(encoding="utf-8")
