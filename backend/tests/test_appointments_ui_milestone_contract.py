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


def test_appointment_detail_uses_single_edit_surface_and_preserves_current_defaults() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/appointments/[appointmentId]/page.tsx").read_text(encoding="utf-8")
    editor = (_root() / "frontend/src/app/(dashboard)/appointments/[appointmentId]/service-editor.tsx").read_text(encoding="utf-8")

    assert "/reschedule" not in page
    assert "AppointmentServiceEditor" in page
    assert 'key={`${appointment.service_id}:${appointment.doctor_id}:' in page
    assert "useEffect" not in editor
    assert "service.is_active || service.id === currentServiceId" in editor
    assert "item.is_active || item.id === appointment.doctor_id" in page
    assert 'type="datetime-local"' not in editor
    assert "getAppointmentEditAvailability" in editor
    assert "عرض المواعيد المتاحة" in editor
    assert "من {timeLabel(slot.start_at" in editor
    assert "إلى {timeLabel(slot.end_at" in editor
    assert "setServiceId(currentServiceId)" in editor
    assert "setDoctorId(currentDoctorId)" in editor


def test_appointment_schedule_has_no_other_column_or_working_hours_banner() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/appointments/page.tsx").read_text(encoding="utf-8")

    schedule_contract = page.split("type ScheduleColumnId", 1)[1].split("function requestedColumns", 1)[0]
    assert '"other"' not in schedule_contract
    assert 'label: "أخرى"' not in schedule_contract
    assert "ساعات العمل:" not in page
