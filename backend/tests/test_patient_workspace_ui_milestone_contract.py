from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_patient_list_preserves_search_filters_and_responsive_views() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/page.tsx").read_text(encoding="utf-8")

    assert 'name="q"' in page
    assert 'name="status"' in page
    assert 'name="source"' in page
    assert 'md:hidden' in page
    assert 'hidden md:block' in page
    assert '<StatusBadge domain="patient"' in page
    assert "hasActiveFilters = Boolean(q || status || source)" in page
    assert 'href="/patients"' in page


def test_patient_workspace_prioritizes_current_context_before_history() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(encoding="utf-8")

    assert "الخطوة القادمة" in page
    assert "يحتاج انتباه" in page
    assert "آخر تواصل" in page
    assert 'order-2 xl:order-1' in page
    assert 'order-1 space-y-5 xl:order-2' in page
    assert "function nextOperationalAction" in page
    assert "Date.parse(taskAt) <= Date.parse(appointmentAt)" in page
    assert 'nextAction?.kind === "task"' in page
    assert 'nextAction?.kind === "appointment"' in page
    assert "/tasks?scope=all&patient_id=" in page
    assert "/appointments?patient_id=" in page
    assert "stats.overdue_tasks > 0" in page
    assert "stats.active_handoffs > 0" in page
    assert "/inbox/" in page


def test_patient_workspace_preserves_crm_and_entitlement_actions() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(encoding="utf-8")
    actions = (_root() / "frontend/src/app/(dashboard)/patients/actions.ts").read_text(encoding="utf-8")
    packages = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/package-panel.tsx").read_text(encoding="utf-8")
    pulses = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/pulse-panel.tsx").read_text(encoding="utf-8")

    for capability in ("createPatientTask", "addPatientNote", "PatientPackagePanel", "PatientPulsePanel"):
        assert capability in page
    assert "setPatientWhatsappOptIn" not in page
    assert "موافقة تواصل واتساب" not in page
    for capability in ("purchasePatientPackage", "recordPatientPackagePayment"):
        assert capability in actions and capability in packages
    assert "cancelPatientPackage" in actions
    assert "PackageCancellationForm" in packages
    for capability in ("purchasePatientPulsePack", "recordPatientPulsePackPayment"):
        assert capability in actions and capability in pulses
    assert "sessions_remaining" in packages
    assert "balance_due_minor" in packages
    assert "pulses_remaining" in pulses
    assert "device_name" in pulses


def test_patient_timeline_keeps_operational_truth_and_deep_links() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(encoding="utf-8")

    for capability in ("const message = event.message", "const note = event.note", "const appointment = event.appointment", "const handoff = event.handoff", "const task = event.task", "const payment = event.payment"):
        assert capability in page
    assert 'event.kind === "appointment_status"' in page
    assert "actorLabel(event)" in page
    assert "event.occurred_at" in page
    assert "message.delivery_status" in page
    assert "payment.transaction_type" in page
    assert "note?.is_pinned" in page
    assert "مثبتة" in page
    assert ">مهمة للفريق</span>" not in page


def test_patient_contextual_booking_and_task_filter_use_existing_contracts() -> None:
    appointments = (_root() / "frontend/src/app/(dashboard)/appointments/page.tsx").read_text(encoding="utf-8")
    tasks = (_root() / "frontend/src/app/(dashboard)/tasks/page.tsx").read_text(encoding="utf-8")
    pulses = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/pulse-panel.tsx").read_text(encoding="utf-8")

    assert 'book?: string' in appointments
    assert 'open={raw.book === "1"}' in appointments
    assert 'mode="existing"' in appointments
    assert "packages={selectedPackages}" in appointments
    assert "pulseBalances={selectedPulseBalances}" in appointments
    assert 'patient_id?: string' in tasks
    assert 'query.set("patient_id", filters.patient_id)' in tasks
    assert "teal-" not in pulses


def test_patient_list_does_not_explain_active_status_inline() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/page.tsx").read_text(encoding="utf-8")

    assert "«نشط» لا تعني تلقائيًا" not in page
    assert "آخر تواصل ظاهر في عمود منفصل" not in page
