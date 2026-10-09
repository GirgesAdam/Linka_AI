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
    assert "الحجز السريع الاستثنائي" in mobile
    assert 'visibleColumns.includes("quick")' in mobile
    assert 'quickBookingHref(currentParams, selectedDate, branchId, "quick"' in mobile
    assert "available appointment times" not in mobile
    assert "formatMoney" not in mobile
    assert "price_minor" not in mobile


def test_appointments_schedule_keeps_exceptional_quick_mode_isolated_from_normal_period_dialogs() -> None:
    root = _root()
    page = (root / "frontend/src/app/(dashboard)/appointments/page.tsx").read_text(encoding="utf-8")
    dialog = (root / "frontend/src/app/(dashboard)/appointments/quick-appointment-dialog.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/appointments/actions.ts").read_text(encoding="utf-8")

    assert "quickBookingHref" in page
    assert "allowQuickBooking" in page
    assert 'quickBookingHref(currentParams, selectedDate, branchId, "quick"' in page
    assert 'quickBookingHref(currentParams, selectedDate, branchId, column.id' in page
    assert 'schedulingMode={column === "quick" ? "quick" : "standard"}' in dialog
    assert 'bookingMode === "quick" ? "/booking/appointments/quick" : "/booking/appointments"' in actions
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
