from pathlib import Path

from app.api.routes.doctor_admin import (
    DoctorAdminCreate,
    DoctorAdminUpdate,
    _stored_doctor_name,
)

ROOT = Path(__file__).resolve().parents[2]


def test_fresh_existing_appointment_service_edit_uses_reschedule_semantics() -> None:
    interpreter = (ROOT / "backend/app/agents/turn_interpreter.py").read_text(encoding="utf-8")
    agent_chat = (ROOT / "backend/app/services/agent_chat.py").read_text(encoding="utf-8")
    assert "EXISTING APPOINTMENT EDITS" in interpreter
    assert "treat it as appointment_reschedule even when the requested date/time stays" in interpreter
    assert "clinic_catalog = _with_current_patient_appointments(" in agent_chat
    assert '"laser_device_key": appointment.laser_device_key' in agent_chat


def test_doctor_calendar_is_bounded_and_workspace_scoped() -> None:
    route = (ROOT / "backend/app/api/routes/doctor_calendar.py").read_text(encoding="utf-8")
    assert "Doctor calendar ranges cannot exceed 42 days" in route
    assert "Appointment.workspace_id == access.workspace.id" in route
    assert "Appointment.status.notin_" in route


def test_service_editor_handles_conflicts_inline() -> None:
    actions = (ROOT / "frontend/src/app/(dashboard)/appointments/[appointmentId]/actions.ts").read_text(encoding="utf-8")
    editor = (ROOT / "frontend/src/app/(dashboard)/appointments/[appointmentId]/service-editor.tsx").read_text(encoding="utf-8")
    assert "AppointmentServiceChangeState" in actions
    assert "catch (error)" in actions
    assert "useActionState" in editor
    assert "state.error" in editor
    assert "exclude_appointment_id: appointmentId" in actions
    assert "doctor_id: doctorId" in actions
    assert 'allow_immediate: "true"' in actions
    error_mapper = actions.split("function serviceChangeError", 1)[1].split(
        "export async function changeAppointmentService", 1
    )[0]
    assert "Ø" not in error_mapper


def test_doctor_admin_is_admin_only_and_branchless_in_the_ui() -> None:
    route = (ROOT / "backend/app/api/routes/doctor_admin.py").read_text(encoding="utf-8")
    actions = (ROOT / "frontend/src/app/(dashboard)/doctors/actions.ts").read_text(encoding="utf-8")
    panel = (ROOT / "frontend/src/app/(dashboard)/doctors/doctor-management.tsx").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/app/(dashboard)/doctors/page.tsx").read_text(encoding="utf-8")
    router = (ROOT / "backend/app/api/router.py").read_text(encoding="utf-8")

    assert "get_workspace_admin" in route
    assert '"/doctor-admin/{doctor_id}/working-hours"' in route
    assert "branch_id" not in DoctorAdminCreate.model_fields
    assert "branch_id" not in actions
    assert "الفرع" not in panel
    assert 'ctx.workspace.role === "admin"' in page
    assert "DoctorManagementPanel" in page
    assert "doctor_admin_read_router" in router


def test_doctor_admin_profile_has_no_required_user_fields_and_accepts_legacy_payloads() -> None:
    create = DoctorAdminCreate()
    update = DoctorAdminUpdate()

    assert create.name is None
    assert create.phone is None
    assert create.specialization is None
    assert create.service_ids == []
    assert update.name is None
    assert update.phone is None
    assert update.specialization is None
    assert update.service_ids == []

    assert create.first_name is None
    assert create.last_name is None
    assert create.email is None
    assert update.first_name is None
    assert update.last_name is None
    assert update.email is None

    assert _stored_doctor_name(name=None, first_name=None, last_name=None) == ("", "")
    assert _stored_doctor_name(
        name="  د. سارة منصور  ", first_name="legacy", last_name="ignored"
    ) == ("د. سارة منصور", "")
    assert _stored_doctor_name(
        name=None, first_name=" أحمد ", last_name=" محمود "
    ) == ("أحمد", "محمود")


def test_doctor_admin_ui_uses_one_optional_name_and_keeps_editor_mounted_after_save() -> None:
    actions = (ROOT / "frontend/src/app/(dashboard)/doctors/actions.ts").read_text(encoding="utf-8")
    panel = (ROOT / "frontend/src/app/(dashboard)/doctors/doctor-management.tsx").read_text(encoding="utf-8")
    read_route = (ROOT / "backend/app/api/routes/doctor_admin_read.py").read_text(encoding="utf-8")

    profile_action = actions.split("export async function updateDoctorAction", 1)[1].split(
        "export async function updateDoctorScheduleAction", 1
    )[0]
    schedule_action = actions.split("export async function updateDoctorScheduleAction", 1)[1].split(
        "export async function removeDoctorAction", 1
    )[0]

    assert 'name="name"' in panel
    assert 'name="first_name"' not in panel
    assert 'name="last_name"' not in panel
    assert 'name="email"' not in panel
    assert "required" not in panel
    assert 'revalidatePath("/doctors")' not in profile_action
    assert 'revalidatePath("/doctors")' not in schedule_action
    assert "refreshDoctorRelatedViews();" in profile_action

    # Keep the previous deployed frontend safe until the intentionally manual Vercel rollout.
    assert "first_name: str | None" in read_route
    assert "last_name: str | None" in read_route
    assert "email: str | None" in read_route
    assert "first_name=staff.first_name" in read_route
    assert "last_name=staff.last_name" in read_route
    assert "email=None" in read_route


def test_doctor_delete_is_soft_and_blocks_doctors_with_upcoming_visits() -> None:
    route = (ROOT / "backend/app/api/routes/doctor_admin.py").read_text(encoding="utf-8")
    assert "Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES)" in route
    assert "Appointment.start_at >= datetime.now(UTC)" in route
    assert "upcoming appointments" in route
    assert "doctor.is_active = False" in route
    assert "doctor.booking_enabled = False" in route
    assert 'action="clinic.doctor_archived"' in route
    assert '"soft_delete": True' in route
