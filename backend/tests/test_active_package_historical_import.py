
from __future__ import annotations

import base64
import io
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.service import Service
from app.models.staff import Staff
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.crm import normalize_patient_identity_phone
from app.schemas.historical_import import HistoricalImportDocument
from app.services import historical_import as history
from app.services import patient_packages as package_service
from app.services.agent_v2.package_booking_policy import resolve_booking_package
from app.services.agent_v2.planner import PlanStep, ReadRequest
from app.services.agent_v2.read_executor import ReadExecutionContext, execute_step_reads

CAIRO = ZoneInfo("Africa/Cairo")


@contextmanager
def _db_session():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    connection = engine.connect()
    outer = connection.begin()
    db = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield db
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
        engine.dispose()


def _seed_workspace(db: Session, *, services: tuple[str, ...] = ("Hydrafacial",)):
    suffix = uuid4().hex[:12]
    user = User(
        email=f"active-package-{suffix}@example.com",
        full_name="Import Test",
        is_active=True,
    )
    workspace = Workspace(
        name=f"Active Package {suffix}",
        slug=f"active-package-{suffix}",
        timezone="Africa/Cairo",
        is_active=True,
        is_demo=True,
    )
    db.add_all([user, workspace])
    db.flush()

    branch = Branch(
        workspace_id=workspace.id,
        name="Main",
        code="MAIN",
        phone=None,
        email=None,
        address_line1=None,
        address_line2=None,
        city="Cairo",
        state=None,
        country_code="EG",
        timezone="Africa/Cairo",
        is_active=True,
    )
    db.add(branch)
    db.flush()
    workspace.primary_branch_id = branch.id

    service_rows: dict[str, Service] = {}
    for index, name in enumerate(services, start=1):
        service = Service(
            workspace_id=workspace.id,
            name=name,
            slug=f"{name.casefold().replace(' ', '-')}-{index}-{suffix}",
            category="Skin",
            operational_category="dermatology",
            description=None,
            duration_minutes=60,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            price_minor=180_000,
            currency="EGP",
            requires_medical_review=False,
            requires_laser_device=False,
            is_active=True,
        )
        db.add(service)
        db.flush()
        service_rows[name] = service

    staff = Staff(
        workspace_id=workspace.id,
        user_id=None,
        first_name="Dr.",
        last_name="Test",
        email=None,
        phone=None,
        job_title="doctor",
        is_active=True,
    )
    db.add(staff)
    db.flush()
    doctor = Doctor(
        workspace_id=workspace.id,
        staff_id=staff.id,
        doctor_type="regular",
        specialization=None,
        license_number=None,
        bio=None,
        booking_enabled=True,
        is_active=True,
    )
    db.add(doctor)
    db.flush()
    return workspace, user, branch, service_rows, doctor


def _active_package_document(
    rows: list[list[object]],
    *,
    include_package_price: bool = True,
) -> HistoricalImportDocument:
    wb = Workbook()
    readme = wb.active
    readme.title = "README"
    readme.append(["test"])
    sheet = wb.create_sheet("active_packages")
    headers = [
        "full_name",
        "phone",
        "service_name",
        "sessions_total",
        "sessions_remaining",
        "amount_paid",
        "purchased_at",
    ]
    if include_package_price:
        headers.append("package_price")
    sheet.append(headers)
    for row in rows:
        sheet.append(row[: len(headers)])
    stream = io.BytesIO()
    wb.save(stream)
    wb.close()
    return HistoricalImportDocument(
        name="active_packages.xlsx",
        format="xlsx",
        content_base64=base64.b64encode(stream.getvalue()).decode("ascii"),
    )


def _legacy_document() -> HistoricalImportDocument:
    wb = Workbook()
    patients = wb.active
    patients.title = "patients"
    patients.append(["full_name", "phone"])
    patients.append(["Legacy Patient", "01019999999"])
    packages = wb.create_sheet("packages")
    packages.append(
        [
            "patient_phone",
            "service_name",
            "sessions_total",
            "sessions_remaining",
            "price",
            "purchased_at",
            "status",
        ]
    )
    packages.append(
        ["01019999999", "Hydrafacial", 6, 2, 3000, "2026-06-01", "active"]
    )
    stream = io.BytesIO()
    wb.save(stream)
    wb.close()
    return HistoricalImportDocument(
        name="legacy.xlsx",
        format="xlsx",
        content_base64=base64.b64encode(stream.getvalue()).decode("ascii"),
    )


def _preview_and_apply(
    db: Session,
    *,
    workspace: Workspace,
    user: User,
    document: HistoricalImportDocument,
    mode: str = "append",
):
    preview = history.preview_historical_import(
        db,
        workspace=workspace,
        user_id=user.id,
        documents=[document],
        mode=mode,
    )
    batch = history.get_historical_import_batch(
        db,
        workspace_id=workspace.id,
        batch_id=preview.batch.batch_id,
    )
    assert batch is not None
    summary = history.apply_historical_import(
        db,
        workspace=workspace,
        batch=batch,
    )
    return preview, summary


@pytest.mark.parametrize(
    ("value", "expected_display", "expected_normalized"),
    [
        ("01012345678", "01012345678", "+201012345678"),
        (1012345678, "01012345678", "+201012345678"),
        ("1012345678", "01012345678", "+201012345678"),
        ("+201012345678", "01012345678", "+201012345678"),
        ("201012345678", "01012345678", "+201012345678"),
        ("00201012345678", "01012345678", "+201012345678"),
    ],
)
def test_active_package_egypt_phone_normalization_repairs_excel_leading_zero(
    value,
    expected_display: str,
    expected_normalized: str,
) -> None:
    display, normalized = history._normalize_active_package_egypt_phone(value)
    assert display == expected_display
    assert normalized == expected_normalized


@pytest.mark.parametrize(
    "value",
    [
        "12",
        "abc",
        "01312345678",
        "+971501234567",
        "0212345678",
    ],
)
def test_active_package_phone_rejects_non_egyptian_mobile_numbers(value) -> None:
    with pytest.raises(ValueError):
        history._normalize_active_package_egypt_phone(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15/2/2026", "2026-02-15"),
        ("15-02-2026", "2026-02-15"),
        ("15.02.2026", "2026-02-15"),
        ("2026-02-15", "2026-02-15"),
        ("2026/02/15", "2026-02-15"),
        ("2026.02.15", "2026-02-15"),
        ("2026-02-15T09:30:00", "2026-02-15"),
        (datetime(2026, 2, 15, 9, 30), "2026-02-15"),
    ],
)
def test_active_package_purchase_date_accepts_excel_and_clear_egyptian_formats(
    value,
    expected: str,
) -> None:
    parsed = history._parse_active_package_purchase_date(value)
    assert parsed is not None
    assert parsed.isoformat() == expected


@pytest.mark.parametrize("value", ["02/15/2026", "not-a-date", 46000])
def test_active_package_purchase_date_rejects_ambiguous_or_raw_numeric_values(value) -> None:
    assert history._parse_active_package_purchase_date(value) is None


def test_active_package_template_is_arabic_guided_and_uses_exact_active_services() -> None:
    payload = history.build_historical_import_template(
        service_names=["Hydrafacial", "ليزر إزالة الشعر - جسم كامل سيدات"]
    )
    workbook = load_workbook(io.BytesIO(payload))
    try:
        assert workbook.sheetnames == ["README", "active_packages"]
        readme = workbook["README"]
        sheet = workbook["active_packages"]

        assert readme.sheet_view.rightToLeft is True
        assert readme["A1"].value == "دليل استيراد الباقات النشطة إلى Linka"
        assert readme["A13"].value == "مثال صف صحيح — امشِ على نفس الشكل"
        assert [readme.cell(14, column).value for column in range(1, 9)] == [
            "full_name",
            "phone",
            "service_name",
            "sessions_total",
            "sessions_remaining",
            "amount_paid",
            "purchased_at",
            "package_price",
        ]
        assert readme["A15"].value == "سارة أحمد"
        assert readme["B15"].value == "01012345678"
        assert readme["B15"].number_format == "@"
        assert readme["G15"].value == "15/02/2026"

        readme_text = "\n".join(
            str(cell.value)
            for row in readme.iter_rows()
            for cell in row
            if cell.value is not None
        )
        assert "لو Excel حذف الصفر الأول Linka تصلحه تلقائيًا" in readme_text
        assert "الخدمات بالأسفل هي نفس الخدمات النشطة المسجلة حاليًا داخل Linka لهذه العيادة" in readme_text
        assert "Linka تربط الباقة بنفس الخدمة الفعلية داخل النظام" in readme_text
        assert "Hydrafacial" in readme_text
        assert "ليزر إزالة الشعر - جسم كامل سيدات" in readme_text

        assert sheet["B2"].number_format == "@"
        assert sheet["G2"].number_format == "dd/mm/yyyy"
        validations = list(sheet.data_validations.dataValidation)
        assert len(validations) == 1
        validation = validations[0]
        assert "README" in validation.formula1
        assert str(validation.sqref) == "C2:C5000"
        assert validation.error == "اختر خدمة من قائمة الخدمات النشطة الموجودة في README."
    finally:
        workbook.close()


def test_active_package_all_invalid_rows_return_actionable_preview() -> None:
    with _db_session() as db:
        workspace, user, _branch, _services, _doctor = _seed_workspace(db)
        preview = history.preview_historical_import(
            db,
            workspace=workspace,
            user_id=user.id,
            documents=[
                _active_package_document(
                    [[
                        "Bad Phone",
                        12,
                        "Hydrafacial",
                        6,
                        3,
                        3000,
                        "15/2/2026",
                        None,
                    ]]
                )
            ],
            mode="append",
        )
        assert preview.can_import is False
        assert preview.ready_counts.get("package", 0) == 0
        assert preview.rejected_counts["package"] == 1
        assert len(preview.issue_groups) == 1
        issue = preview.issue_groups[0]
        assert issue.code == "active_package_phone_invalid"
        assert issue.example_rows == [2]
        assert "موبايل مصري" in issue.message


@pytest.mark.usefixtures("monkeypatch")
def test_active_package_happy_path_financials_and_opening_balance(monkeypatch) -> None:
    monkeypatch.setattr(history, "record_activity_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(package_service, "record_activity_event", lambda *args, **kwargs: None)

    with _db_session() as db:
        workspace, user, branch, services, doctor = _seed_workspace(db)
        service = services["Hydrafacial"]
        document = _active_package_document(
            [
                [
                    "سارة أحمد",
                    "01012345678",
                    "Hydrafacial",
                    10,
                    4,
                    6000,
                    "2026-07-15",
                    None,
                ]
            ],
            include_package_price=False,
        )

        preview, _summary = _preview_and_apply(
            db,
            workspace=workspace,
            user=user,
            document=document,
        )
        assert preview.ready_counts == {"package": 1}
        assert preview.rejected_counts == {}
        assert preview.projected_counts == {
            "patients_to_create": 1,
            "existing_patients_reused": 0,
            "packages_to_create": 1,
            "historical_payments_to_create": 1,
        }
        assert preview.total_amount_imported_minor == 600_000

        _display, normalized = normalize_patient_identity_phone("01012345678")
        patient = db.scalar(
            select(Patient).where(
                Patient.workspace_id == workspace.id,
                Patient.phone_normalized == normalized,
            )
        )
        assert patient is not None
        assert f"{patient.first_name} {patient.last_name or ''}".strip() == "سارة أحمد"

        package = db.scalar(
            select(PatientPackage).where(
                PatientPackage.workspace_id == workspace.id,
                PatientPackage.patient_id == patient.id,
            )
        )
        assert package is not None
        assert package.service_id == service.id
        assert package.sessions_purchased == 10
        assert package.opening_sessions_remaining == 4
        assert package.sessions_total_known is True
        assert package.sale_price_minor == 600_000
        assert package.expires_at is None
        assert package.status == "active"
        assert package.source == "integration"
        assert package.purchase_transaction_id is not None
        assert db.scalar(
            select(func.count())
            .select_from(PackageUsage)
            .where(PackageUsage.patient_package_id == package.id)
        ) == 0

        payment = db.get(PaymentTransaction, package.purchase_transaction_id)
        assert payment is not None
        assert payment.patient_id == patient.id
        assert payment.patient_package_id == package.id
        assert payment.transaction_type == "payment"
        assert payment.amount_minor == 600_000
        assert payment.currency == "EGP"
        assert payment.payment_method == "unknown"
        assert payment.source == "integration"
        assert payment.created_at == package.purchased_at

        financial = package_service.package_read(db, package, include_financials=True)
        assert financial.sessions_remaining == 4
        assert financial.amount_paid_minor == 600_000
        assert financial.balance_due_minor == 0
        assert financial.effective_status == "active"

        agent_read = execute_step_reads(
            PlanStep(
                operation_index=0,
                operation_type="package_info",
                disposition="read",
                reads=[
                    ReadRequest(
                        kind="customer_packages",
                        parameters={"service_id": str(service.id)},
                    )
                ],
                response_goal="package_information",
            ),
            ReadExecutionContext(
                db=db,
                workspace=workspace,
                patient=patient,
                now=datetime.now(UTC),
            ),
        )
        agent_packages = agent_read.results[0].payload["packages"]
        assert len(agent_packages) == 1
        assert agent_packages[0]["sessions_remaining"] == 4

        start_at = datetime.now(UTC) + timedelta(days=10)
        booking_resolution = resolve_booking_package(
            db,
            workspace=workspace,
            patient=patient,
            service_id=service.id,
            start_at=start_at,
            device_key=None,
            package_usage="use_existing",
        )
        assert booking_resolution.package_used is True
        assert booking_resolution.package_id == package.id
        end_at = start_at + timedelta(minutes=60)
        package_service.validate_package_for_booking(
            db,
            workspace_id=workspace.id,
            package_id=package.id,
            patient_id=patient.id,
            service_id=service.id,
            appointment_start_at=start_at,
        )
        appointment = Appointment(
            workspace_id=workspace.id,
            patient_id=patient.id,
            branch_id=branch.id,
            doctor_id=doctor.id,
            doctor_assignment_known=True,
            is_quick_booking=False,
            service_id=service.id,
            patient_package_id=None,
            visit_group_id=None,
            lead_id=None,
            created_by_user_id=user.id,
            rescheduled_from_appointment_id=None,
            status="confirmed",
            source="staff",
            start_at=start_at,
            end_at=end_at,
            busy_start_at=start_at,
            busy_end_at=end_at,
            duration_minutes=60,
            price_minor=service.price_minor,
            discount_minor=0,
            currency="EGP",
            laser_device_key=None,
            laser_device_name=None,
            laser_pulses_used=None,
            payment_status="unknown",
            amount_paid_minor=None,
            payment_method="unknown",
            billing_context="standard",
            package_external_id=None,
            customer_note=None,
            cancellation_reason=None,
            idempotency_key=None,
        )
        db.add(appointment)
        db.flush()
        package_service.reserve_package_usage(
            db,
            appointment=appointment,
            package=package,
            actor_user_id=user.id,
        )
        after = package_service.package_read(db, package, include_financials=True)
        assert after.sessions_remaining == 3
        assert after.amount_paid_minor == 600_000
        assert after.balance_due_minor == 0


def test_active_package_partial_payment_patient_reuse_multiple_and_reimport(monkeypatch) -> None:
    monkeypatch.setattr(history, "record_activity_event", lambda *args, **kwargs: None)
    with _db_session() as db:
        workspace, user, _branch, services, _doctor = _seed_workspace(db)
        service = services["Hydrafacial"]
        display, normalized = normalize_patient_identity_phone("01012345678")
        existing = Patient(
            workspace_id=workspace.id,
            first_name="Existing",
            last_name="CRM",
            phone=display,
            phone_normalized=normalized,
            gender="female",
            preferred_language="ar",
            preferred_branch_id=workspace.primary_branch_id,
            source="referral",
            status="active",
            marketing_consent=False,
        )
        db.add(existing)
        db.flush()

        rows = [
            [
                "Different Excel Name",
                "01012345678",
                "Hydrafacial",
                10,
                4,
                5000,
                "2026-07-15",
                8000,
            ],
            [
                "Different Excel Name",
                "+201012345678",
                "Hydrafacial",
                6,
                2,
                3000,
                "2026-08-15",
                None,
            ],
        ]
        document = _active_package_document(rows)

        preview, _summary = _preview_and_apply(
            db,
            workspace=workspace,
            user=user,
            document=document,
        )
        assert preview.projected_counts == {
            "patients_to_create": 0,
            "existing_patients_reused": 1,
            "packages_to_create": 2,
            "historical_payments_to_create": 2,
        }
        assert preview.total_amount_imported_minor == 800_000

        patients = list(
            db.scalars(
                select(Patient).where(
                    Patient.workspace_id == workspace.id,
                    Patient.phone_normalized == normalized,
                )
            ).all()
        )
        assert len(patients) == 1
        assert patients[0].id == existing.id
        assert patients[0].first_name == "Existing"
        assert patients[0].last_name == "CRM"

        packages = list(
            db.scalars(
                select(PatientPackage)
                .where(
                    PatientPackage.workspace_id == workspace.id,
                    PatientPackage.patient_id == existing.id,
                    PatientPackage.service_id == service.id,
                )
                .order_by(PatientPackage.purchased_at)
            ).all()
        )
        assert len(packages) == 2
        payments = list(
            db.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.workspace_id == workspace.id,
                    PaymentTransaction.patient_package_id.in_([row.id for row in packages]),
                )
            ).all()
        )
        assert len(payments) == 2

        partial = next(row for row in packages if row.sale_price_minor == 800_000)
        financial = package_service.package_read(db, partial, include_financials=True)
        assert financial.amount_paid_minor == 500_000
        assert financial.balance_due_minor == 300_000

        renamed_rows = [list(row) for row in rows]
        renamed_rows[0][0] = "Another Historical Name"
        renamed_rows[1][0] = "Another Historical Name"
        preview2, _summary2 = _preview_and_apply(
            db,
            workspace=workspace,
            user=user,
            document=_active_package_document(renamed_rows),
        )
        assert preview2.projected_counts["packages_to_create"] == 0
        assert preview2.projected_counts["historical_payments_to_create"] == 0
        assert preview2.total_amount_imported_minor == 0
        assert db.scalar(
            select(func.count())
            .select_from(PatientPackage)
            .where(PatientPackage.workspace_id == workspace.id)
        ) == 2
        assert db.scalar(
            select(func.count())
            .select_from(PaymentTransaction)
            .where(PaymentTransaction.workspace_id == workspace.id)
        ) == 2

        reordered = _active_package_document(list(reversed(rows)))
        preview3, _summary3 = _preview_and_apply(
            db,
            workspace=workspace,
            user=user,
            document=reordered,
        )
        assert preview3.projected_counts["packages_to_create"] == 0
        assert preview3.projected_counts["historical_payments_to_create"] == 0
        assert db.scalar(
            select(func.count())
            .select_from(PatientPackage)
            .where(PatientPackage.workspace_id == workspace.id)
        ) == 2
        assert db.scalar(
            select(func.count())
            .select_from(PaymentTransaction)
            .where(PaymentTransaction.workspace_id == workspace.id)
        ) == 2


def test_active_package_preview_rejects_invalid_and_duplicate_rows_without_writes() -> None:
    with _db_session() as db:
        workspace, user, _branch, _services, _doctor = _seed_workspace(db)
        valid = [
            "Valid Patient",
            "01011111111",
            "Hydrafacial",
            10,
            4,
            6000,
            "2026-07-15",
            None,
        ]
        rows = [
            valid,
            list(valid),
            ["Unknown", "01011111112", "Unknown", 10, 4, 6000, "2026-07-15", None],
            ["Zero Total", "01011111113", "Hydrafacial", 0, 0, 6000, "2026-07-15", None],
            ["Negative Rem", "01011111114", "Hydrafacial", 10, -1, 6000, "2026-07-15", None],
            ["Too Much Rem", "01011111115", "Hydrafacial", 10, 11, 6000, "2026-07-15", None],
            ["Zero Paid", "01011111116", "Hydrafacial", 10, 4, 0, "2026-07-15", None],
            ["Negative Paid", "01011111117", "Hydrafacial", 10, 4, -1, "2026-07-15", None],
            ["Low Price", "01011111118", "Hydrafacial", 10, 4, 6000, "2026-07-15", 5000],
            ["Bad Date", "01011111119", "Hydrafacial", 10, 4, 6000, "not-a-date", None],
            ["Bad Phone", "abc", "Hydrafacial", 10, 4, 6000, "2026-07-15", None],
        ]
        preview = history.preview_historical_import(
            db,
            workspace=workspace,
            user_id=user.id,
            documents=[_active_package_document(rows)],
            mode="append",
        )
        assert preview.ready_counts["package"] == 1
        assert preview.rejected_counts["package"] == len(rows) - 1
        codes = {issue.code for issue in preview.issue_groups}
        assert {
            "duplicate_historical_package_row",
            "active_package_service_unknown",
            "active_package_total_invalid",
            "active_package_remaining_invalid",
            "active_package_remaining_exceeds_total",
            "active_package_amount_paid_invalid",
            "active_package_price_below_paid",
            "active_package_purchase_date_invalid",
            "active_package_phone_invalid",
        }.issubset(codes)
        assert db.scalar(
            select(func.count())
            .select_from(Patient)
            .where(Patient.workspace_id == workspace.id)
        ) == 0
        assert db.scalar(
            select(func.count())
            .select_from(PatientPackage)
            .where(PatientPackage.workspace_id == workspace.id)
        ) == 0
        assert db.scalar(
            select(func.count())
            .select_from(PaymentTransaction)
            .where(PaymentTransaction.workspace_id == workspace.id)
        ) == 0


def test_legacy_multisheet_workbook_remains_previewable_and_importable(monkeypatch) -> None:
    monkeypatch.setattr(history, "record_activity_event", lambda *args, **kwargs: None)
    with _db_session() as db:
        workspace, user, _branch, _services, _doctor = _seed_workspace(db)
        preview, _summary = _preview_and_apply(
            db,
            workspace=workspace,
            user=user,
            document=_legacy_document(),
        )
        assert preview.batch.schema_version == "tia_history_v1"
        assert preview.ready_counts["patient"] == 1
        assert preview.ready_counts["package"] == 1
        assert db.scalar(
            select(func.count())
            .select_from(Patient)
            .where(Patient.workspace_id == workspace.id)
        ) == 1
        package = db.scalar(
            select(PatientPackage).where(PatientPackage.workspace_id == workspace.id)
        )
        assert package is not None
        assert package.opening_sessions_remaining == 2
        assert package.source == "integration"
