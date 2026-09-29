from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_appointments_mobile_uses_chronological_agenda_without_financial_data() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/appointments/page.tsx").read_text(encoding="utf-8")

    assert "function MobileAgenda" in page
    assert 'className="space-y-4 lg:hidden"' in page
    assert 'className="hidden space-y-4 lg:block"' in page
    assert '<StatusBadge domain="appointment" status={appointment.status}' in page
    mobile = page.split("function MobileAgenda", 1)[1].split("function DailySchedule", 1)[0]
    assert "available appointment times" in mobile
    assert "quickBookingHref" in mobile
    assert "formatMoney" not in mobile
    assert "price_minor" not in mobile


def test_appointments_schedule_keeps_quick_booking_on_desktop_free_periods() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/appointments/page.tsx").read_text(encoding="utf-8")

    assert "quickBookingHref" in page
    assert "allowQuickBooking" in page
    assert 'quickBookingHref(currentParams' in page
    assert "QuickAppointmentDialog" in page
    assert "mobile resource filter" in page
    assert "resourceFilterHref" in page


def test_appointment_workspace_preserves_visit_commerce_and_lifecycle_capabilities() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/appointments/[appointmentId]/page.tsx").read_text(encoding="utf-8")

    required = (
        "AppointmentServiceEditor",
        "AdditionalServiceForm",
        "AppointmentPaymentForm",
        "purchasePackageFromAppointment",
        "purchasePackageForAdditionalService",
        "updateLaserPulses",
        "addAppointmentProduct",
        "refundAppointmentPayment",
        "cancelAppointment",
        "updateAppointmentStatus",
        '<StatusBadge domain="appointment" status={appointment.status}',
    )
    for capability in required:
        assert capability in page
