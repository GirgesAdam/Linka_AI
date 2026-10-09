from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_manual_booking_uses_verified_availability_before_doctor_selection() -> None:
    root = _root()
    form = (
        root
        / "frontend/src/app/(dashboard)/appointments/manual-appointment-form.tsx"
    ).read_text(encoding="utf-8")
    actions = (
        root / "frontend/src/app/(dashboard)/appointments/actions.ts"
    ).read_text(encoding="utf-8")

    assert "getManualAppointmentAvailability" in form
    assert "/booking/availability?" in actions
    assert "الميعاد المتاح" in form
    assert "الدكتور المتاح في الميعاد" in form
    assert "slots.filter((slot) => slot.start_at === startAt)" in form
    assert 'name="start_at" value={startAt}' in form


def test_schedule_periods_reuse_dialog_without_widening_quick_booking() -> None:
    root = _root()
    page = (
        root / "frontend/src/app/(dashboard)/appointments/page.tsx"
    ).read_text(encoding="utf-8")
    dialog = (
        root / "frontend/src/app/(dashboard)/appointments/quick-appointment-dialog.tsx"
    ).read_text(encoding="utf-8")
    actions = (
        root / "frontend/src/app/(dashboard)/appointments/actions.ts"
    ).read_text(encoding="utf-8")

    assert "quickBookingHref" in page
    assert "allowQuickBooking" in page
    assert "الحجز السريع الاستثنائي" in page
    assert 'quickBookingHref(currentParams, selectedDate, branchId, "quick"' in page
    assert 'quickBookingHref(currentParams, selectedDate, branchId, column.id' in page
    assert '{allowQuickBooking && inlineBookingColumns.length > 0 && (' in page
    assert '!allowQuickBooking || column.id === "quick"' in page
    assert "QuickAppointmentDialog" in page
    assert 'role="dialog"' in dialog
    assert "windowStartMinutes" in dialog
    assert "windowEndMinutes" in dialog
    assert "<ManualAppointmentForm" in dialog
    assert 'schedulingMode={column === "quick" ? "quick" : "standard"}' in dialog
    assert 'fixedLaserDeviceKey={column === "quick" ? undefined : fixedLaserDeviceKey}' in dialog
    assert 'allowedOperationalCategory={column === "quick" ? undefined : allowedOperationalCategory}' in dialog
    assert 'bookingMode === "quick" ? "/booking/appointments/quick" : "/booking/appointments"' in actions


def test_phone_search_keeps_manual_booking_panel_open_and_copy_is_short() -> None:
    root = _root()
    page = (
        root / "frontend/src/app/(dashboard)/appointments/page.tsx"
    ).read_text(encoding="utf-8")

    assert 'open={raw.book === "1" || Boolean(manualPhone)}' in page
    assert "إضافة موعد يدوي" not in page
    assert "<Plus size={17} /> إضافة موعد" in page


def test_package_cancellation_does_not_ask_staff_for_a_reason() -> None:
    root = _root()
    form = (
        root
        / "frontend/src/app/(dashboard)/patients/[patientId]/package-cancellation-form.tsx"
    ).read_text(encoding="utf-8")
    actions = (
        root / "frontend/src/app/(dashboard)/patients/actions.ts"
    ).read_text(encoding="utf-8")

    assert "سبب الإلغاء" not in form
    assert 'name="reason"' not in form
    assert 'const reason = "تم إلغاء الباكيدج من ملف العميل";' in actions
