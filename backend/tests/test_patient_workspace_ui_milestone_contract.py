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


def test_patient_workspace_prioritizes_current_context_before_history() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(encoding="utf-8")

    assert "الخطوة القادمة" in page
    assert "يحتاج انتباه" in page
    assert "آخر تواصل" in page
    assert 'order-2 xl:order-1' in page
    assert 'order-1 space-y-5 xl:order-2' in page
    assert "/appointments?patient_id=" in page
    assert "/inbox/" in page


def test_patient_workspace_preserves_crm_and_entitlement_actions() -> None:
    page = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/page.tsx").read_text(encoding="utf-8")
    actions = (_root() / "frontend/src/app/(dashboard)/patients/actions.ts").read_text(encoding="utf-8")
    packages = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/package-panel.tsx").read_text(encoding="utf-8")
    pulses = (_root() / "frontend/src/app/(dashboard)/patients/[patientId]/pulse-panel.tsx").read_text(encoding="utf-8")

    for capability in ("createPatientTask", "addPatientNote", "setPatientWhatsappOptIn", "PatientPackagePanel", "PatientPulsePanel"):
        assert capability in page
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
