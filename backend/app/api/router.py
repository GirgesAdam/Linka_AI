from fastapi import APIRouter

from app.api.routes.agent import router as agent_router
from app.api.routes.analytics import router as analytics_router
from app.api.routes.analytics_views import router as analytics_views_router
from app.api.routes.appointment_commerce import router as appointment_commerce_router
from app.api.routes.appointment_edits import router as appointment_edits_router
from app.api.routes.auth import router as auth_router
from app.api.routes.automations import router as automations_router
from app.api.routes.booking import router as booking_router
from app.api.routes.campaign_analytics import router as campaign_analytics_router
from app.api.routes.channels import router as channels_router
from app.api.routes.clinic import router as clinic_router
from app.api.routes.clinic_knowledge_base import router as clinic_knowledge_base_router
from app.api.routes.clinic_setup_v2 import router as clinic_setup_v2_router
from app.api.routes.crm import router as crm_router
from app.api.routes.crm_task_executor import router as crm_task_executor_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.doctor_admin import router as doctor_admin_router
from app.api.routes.doctor_admin_read import router as doctor_admin_read_router
from app.api.routes.doctor_calendar import router as doctor_calendar_router
from app.api.routes.doctor_laser_weekdays import router as doctor_laser_weekdays_router
from app.api.routes.finance import router as finance_router
from app.api.routes.health import router as health_router
from app.api.routes.inbox import router as inbox_router
from app.api.routes.inbox_media import router as inbox_media_router
from app.api.routes.inbox_outbound import router as inbox_outbound_router
from app.api.routes.inventory import router as inventory_router
from app.api.routes.onboarding import router as onboarding_router
from app.api.routes.operations import router as operations_router
from app.api.routes.package_offers import router as package_offers_router
from app.api.routes.payments import router as payments_router
from app.api.routes.pulse_billing import router as pulse_billing_router
from app.api.routes.whatsapp_setup import router as whatsapp_setup_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(agent_router, prefix="/agent", tags=["agent"])
api_router.include_router(analytics_router, prefix="/analytics", tags=["analytics"])
api_router.include_router(analytics_views_router, prefix="/analytics", tags=["analytics"])
api_router.include_router(campaign_analytics_router, prefix="/analytics", tags=["analytics"])
api_router.include_router(automations_router, prefix="/automations", tags=["automations"])
api_router.include_router(booking_router, prefix="/booking", tags=["booking"])
api_router.include_router(doctor_calendar_router, prefix="/booking", tags=["booking"])
api_router.include_router(appointment_edits_router, prefix="/booking", tags=["booking"])
api_router.include_router(appointment_commerce_router, prefix="/booking", tags=["booking"])
api_router.include_router(package_offers_router, prefix="/booking", tags=["booking"])
api_router.include_router(pulse_billing_router, prefix="/booking", tags=["booking"])
api_router.include_router(channels_router, prefix="/channels", tags=["channels"])
api_router.include_router(
    whatsapp_setup_router,
    prefix="/channels/whatsapp",
    tags=["whatsapp-setup"],
)
api_router.include_router(clinic_router, prefix="/clinic", tags=["clinic"])
api_router.include_router(clinic_knowledge_base_router, prefix="/clinic", tags=["clinic-knowledge"])
api_router.include_router(doctor_admin_router, prefix="/clinic", tags=["clinic"])
api_router.include_router(doctor_admin_read_router, prefix="/clinic", tags=["clinic"])
api_router.include_router(doctor_laser_weekdays_router, prefix="/clinic", tags=["clinic"])
api_router.include_router(clinic_setup_v2_router, prefix="/clinic", tags=["clinic-setup"])
api_router.include_router(crm_router, prefix="/crm", tags=["crm"])
api_router.include_router(crm_task_executor_router, prefix="/crm", tags=["crm"])
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(finance_router, prefix="/finance", tags=["finance"])
api_router.include_router(inbox_router, prefix="/inbox", tags=["inbox"])
api_router.include_router(inbox_media_router, prefix="/inbox", tags=["inbox"])
api_router.include_router(inbox_outbound_router, prefix="/inbox", tags=["inbox"])
api_router.include_router(inventory_router, prefix="/inventory", tags=["inventory"])
api_router.include_router(onboarding_router, prefix="/onboarding", tags=["onboarding"])
api_router.include_router(operations_router, prefix="/operations", tags=["operations"])
api_router.include_router(payments_router, prefix="/payments", tags=["payments"])
