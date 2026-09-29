from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.orm import Session

from app.models.appointment import Appointment
from app.models.crm_campaign import CRMCampaign, CRMCampaignRecipient
from app.models.crm_campaign_conversion import CRMCampaignConversion
from app.models.message_dispatch import MessageDispatch
from app.models.payment_transaction import PaymentAllocation, PaymentTransaction
from app.schemas.campaign_analytics import (
    CampaignAnalyticsCampaignRead,
    CampaignAnalyticsMetrics,
    CampaignAnalyticsOverviewRead,
)
from app.services.campaign_attribution import CAMPAIGN_DIRECT_RESPONSE_WINDOW_DAYS

_CAMPAIGN_STATUSES = ("confirmed", "cancelled")


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def _campaign_scope(*, workspace_id: UUID, since: datetime | None, campaign_id: UUID | None):
    predicates = [
        CRMCampaign.workspace_id == workspace_id,
        CRMCampaign.status.in_(_CAMPAIGN_STATUSES),
        CRMCampaign.confirmed_at.is_not(None),
    ]
    if since is not None:
        predicates.append(CRMCampaign.confirmed_at >= since)
    if campaign_id is not None:
        predicates.append(CRMCampaign.id == campaign_id)
    return predicates


def campaign_analytics_overview(
    db: Session,
    *,
    workspace_id: UUID,
    days: int | None = 90,
    campaign_id: UUID | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> CampaignAnalyticsOverviewRead:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    since = current - timedelta(days=days) if days is not None else None
    scope = _campaign_scope(workspace_id=workspace_id, since=since, campaign_id=campaign_id)

    campaigns = list(
        db.scalars(
            select(CRMCampaign)
            .where(*scope)
            .order_by(CRMCampaign.confirmed_at.desc(), CRMCampaign.id.desc())
            .limit(limit)
        )
    )
    if not campaigns:
        return CampaignAnalyticsOverviewRead(
            period_label="كل التاريخ" if days is None else f"آخر {days} يوم",
            attribution_window_days=CAMPAIGN_DIRECT_RESPONSE_WINDOW_DAYS,
            totals=CampaignAnalyticsMetrics(),
            definitions=_definitions(),
        )

    campaign_ids = [row.id for row in campaigns]

    dispatch_rows = db.execute(
        select(
            CRMCampaignRecipient.campaign_id,
            func.count(MessageDispatch.id).label("dispatch_count"),
            func.sum(case((MessageDispatch.sent_at.is_not(None), 1), else_=0)).label("sent_count"),
            func.sum(case((MessageDispatch.delivered_at.is_not(None), 1), else_=0)).label("delivered_count"),
            func.sum(case((MessageDispatch.read_at.is_not(None), 1), else_=0)).label("read_count"),
            func.sum(case((MessageDispatch.status == "failed", 1), else_=0)).label("failed_count"),
            func.sum(case((MessageDispatch.status == "cancelled", 1), else_=0)).label("dispatch_cancelled_count"),
        )
        .outerjoin(
            MessageDispatch,
            and_(
                MessageDispatch.workspace_id == CRMCampaignRecipient.workspace_id,
                MessageDispatch.id == CRMCampaignRecipient.dispatch_id,
            ),
        )
        .where(
            CRMCampaignRecipient.workspace_id == workspace_id,
            CRMCampaignRecipient.campaign_id.in_(campaign_ids),
        )
        .group_by(CRMCampaignRecipient.campaign_id)
    ).all()
    dispatch_by_campaign = {
        row.campaign_id: {
            "dispatch_count": int(row.dispatch_count or 0),
            "sent_count": int(row.sent_count or 0),
            "delivered_count": int(row.delivered_count or 0),
            "read_count": int(row.read_count or 0),
            "failed_count": int(row.failed_count or 0),
            "dispatch_cancelled_count": int(row.dispatch_cancelled_count or 0),
        }
        for row in dispatch_rows
    }

    cancelled_rows = db.execute(
        select(
            CRMCampaignRecipient.campaign_id,
            func.sum(
                case(
                    (
                        CRMCampaignRecipient.status.in_(
                            (
                                "cancelled",
                                "cancelled_no_consent",
                                "cancelled_inactive",
                                "cancelled_no_route",
                            )
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("cancelled_count"),
        )
        .where(
            CRMCampaignRecipient.workspace_id == workspace_id,
            CRMCampaignRecipient.campaign_id.in_(campaign_ids),
        )
        .group_by(CRMCampaignRecipient.campaign_id)
    ).all()
    cancelled_by_campaign = {row.campaign_id: int(row.cancelled_count or 0) for row in cancelled_rows}

    signed_allocation = case(
        (PaymentTransaction.transaction_type == "payment", PaymentAllocation.amount_minor),
        (PaymentTransaction.transaction_type == "refund", -PaymentAllocation.amount_minor),
        else_=0,
    )
    conversion_rows = db.execute(
        select(
            CRMCampaignConversion.campaign_id,
            func.count(distinct(CRMCampaignConversion.id)).label("booking_count"),
            func.count(
                distinct(
                    case(
                        (Appointment.status == "completed", CRMCampaignConversion.id),
                        else_=None,
                    )
                )
            ).label("completed_count"),
            func.coalesce(func.sum(signed_allocation), 0).label("revenue_minor"),
        )
        .join(
            Appointment,
            and_(
                Appointment.workspace_id == CRMCampaignConversion.workspace_id,
                Appointment.id == CRMCampaignConversion.appointment_id,
            ),
        )
        .outerjoin(
            PaymentAllocation,
            and_(
                PaymentAllocation.workspace_id == CRMCampaignConversion.workspace_id,
                PaymentAllocation.appointment_id == CRMCampaignConversion.appointment_id,
            ),
        )
        .outerjoin(
            PaymentTransaction,
            and_(
                PaymentTransaction.workspace_id == PaymentAllocation.workspace_id,
                PaymentTransaction.id == PaymentAllocation.transaction_id,
                PaymentTransaction.currency == "EGP",
            ),
        )
        .where(
            CRMCampaignConversion.workspace_id == workspace_id,
            CRMCampaignConversion.campaign_id.in_(campaign_ids),
        )
        .group_by(CRMCampaignConversion.campaign_id)
    ).all()
    conversion_by_campaign = {
        row.campaign_id: {
            "booking_count": int(row.booking_count or 0),
            "completed_count": int(row.completed_count or 0),
            "revenue_minor": int(row.revenue_minor or 0),
        }
        for row in conversion_rows
    }

    output: list[CampaignAnalyticsCampaignRead] = []
    for campaign in campaigns:
        dispatch = dispatch_by_campaign.get(campaign.id, {})
        conversion = conversion_by_campaign.get(campaign.id, {})
        sent_count = int(dispatch.get("sent_count", 0))
        delivered_count = int(dispatch.get("delivered_count", 0))
        read_count = int(dispatch.get("read_count", 0))
        booking_count = int(conversion.get("booking_count", 0))
        output.append(
            CampaignAnalyticsCampaignRead(
                campaign_id=campaign.id,
                cohort_id=campaign.cohort_id,
                name=campaign.name,
                status=campaign.status,
                template_name=campaign.template_name,
                confirmed_at=campaign.confirmed_at,
                recipient_count=campaign.recipient_count,
                eligible_count=campaign.eligible_count,
                dispatch_count=int(dispatch.get("dispatch_count", 0)),
                sent_count=sent_count,
                delivered_count=delivered_count,
                read_count=read_count,
                failed_count=int(dispatch.get("failed_count", 0)),
                cancelled_count=cancelled_by_campaign.get(campaign.id, int(dispatch.get("dispatch_cancelled_count", 0))),
                delivery_rate=_rate(delivered_count, sent_count),
                read_rate=_rate(read_count, delivered_count),
                tracked_booking_count=booking_count,
                completed_booking_count=int(conversion.get("completed_count", 0)),
                booking_conversion_rate=_rate(booking_count, sent_count),
                attributed_revenue_minor=int(conversion.get("revenue_minor", 0)),
                currency="EGP",
            )
        )

    totals = CampaignAnalyticsMetrics(
        recipient_count=sum(row.recipient_count for row in output),
        eligible_count=sum(row.eligible_count for row in output),
        dispatch_count=sum(row.dispatch_count for row in output),
        sent_count=sum(row.sent_count for row in output),
        delivered_count=sum(row.delivered_count for row in output),
        read_count=sum(row.read_count for row in output),
        failed_count=sum(row.failed_count for row in output),
        cancelled_count=sum(row.cancelled_count for row in output),
        tracked_booking_count=sum(row.tracked_booking_count for row in output),
        completed_booking_count=sum(row.completed_booking_count for row in output),
        attributed_revenue_minor=sum(row.attributed_revenue_minor for row in output),
        currency="EGP",
    )
    totals.delivery_rate = _rate(totals.delivered_count, totals.sent_count)
    totals.read_rate = _rate(totals.read_count, totals.delivered_count)
    totals.booking_conversion_rate = _rate(totals.tracked_booking_count, totals.sent_count)

    return CampaignAnalyticsOverviewRead(
        period_label="كل التاريخ" if days is None else f"آخر {days} يوم",
        attribution_window_days=CAMPAIGN_DIRECT_RESPONSE_WINDOW_DAYS,
        totals=totals,
        campaigns=output,
        definitions=_definitions(),
    )


def _definitions() -> list[str]:
    return [
        "تم الإرسال: الرسائل التي أكد مزود WhatsApp أنها خرجت فعليًا.",
        "معدل الوصول = الرسائل التي وصلت / الرسائل التي تم إرسالها.",
        "معدل القراءة = الرسائل المقروءة / الرسائل التي وصلت.",
        "الحجز المنسوب للحملة يُسجل فقط عندما يرد العميل بعد رسالة حملة مُرسلة ثم تنفذ Linka الحجز داخل نفس المحادثة خلال 30 يومًا.",
        "لو وصل للعميل أكثر من حملة في نفس المحادثة قبل الحجز، يُنسب الحجز لآخر حملة مُرسلة قبل الرد والحجز.",
        "لا ننسب الحجوزات القديمة للحملات بأثر رجعي اعتمادًا على تشابه التواريخ أو هوية العميل فقط؛ لذلك الحملات القديمة قبل تفعيل التتبع قد تظهر 0 حجوزات منسوبة.",
        "الإيراد المنسوب = صافي المدفوعات والمرتجعات بالجنيه المرتبطة صراحةً بالمواعيد المتتبعة للحملة.",
    ]
