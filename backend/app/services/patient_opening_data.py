from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.integrations.clinic.authority import require_tia_patient_fields_writable
from app.models.appointment import Appointment
from app.models.appointment_additional_service import AppointmentAdditionalService
from app.models.clinic_inventory import ClinicLaserDevice
from app.models.patient import Patient
from app.models.patient_package import PackageUsage, PatientPackage
from app.models.payment_transaction import PaymentTransaction
from app.models.pulse_billing import AppointmentPulseSettlement, PatientPulsePack, PulseUsage
from app.models.service_package_offer import ServicePackageOffer
from app.schemas.patient_opening_data import (
    PatientOpeningDataRead,
    PatientOpeningDataUpdate,
    PatientOpeningPackageRead,
    PatientOpeningPackageUpdate,
    PatientOpeningPulseRead,
)
from app.services.activity import record_activity_event
from app.services.package_offers import (
    PackageOfferError,
    PackageOfferNotFound,
    create_patient_package_opening_balance,
)
from app.services.patient_packages import package_read
from app.services.pulse_billing import (
    PulseBillingError,
    PulseBillingNotFound,
    create_patient_pulse_opening_balance,
)


class PatientOpeningDataError(ValueError):
    pass


class PatientOpeningDataNotFound(PatientOpeningDataError):
    pass


class PatientOpeningDataConflict(PatientOpeningDataError):
    pass


def _opening_pulse_predicate():
    return (
        PatientPulsePack.pulse_pack_offer_id.is_(None),
        PatientPulsePack.origin_appointment_id.is_(None),
        PatientPulsePack.purchase_transaction_id.is_(None),
        PatientPulsePack.sale_price_minor == 0,
        PatientPulsePack.status == "active",
    )


def _opening_packages(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    for_update: bool,
) -> list[PatientPackage]:
    stmt = select(PatientPackage).where(
        PatientPackage.workspace_id == workspace_id,
        PatientPackage.patient_id == patient_id,
        PatientPackage.opening_sessions_remaining.is_not(None),
        PatientPackage.status == "active",
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt.order_by(PatientPackage.created_at, PatientPackage.id)).all())


def _opening_pulse_packs(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    for_update: bool,
) -> list[PatientPulsePack]:
    stmt = select(PatientPulsePack).where(
        PatientPulsePack.workspace_id == workspace_id,
        PatientPulsePack.patient_id == patient_id,
        *_opening_pulse_predicate(),
    )
    if for_update:
        stmt = stmt.with_for_update()
    packs = list(db.scalars(stmt.order_by(PatientPulsePack.created_at, PatientPulsePack.id)).all())
    seen: set[str] = set()
    for pack in packs:
        key = pack.device_key.casefold()
        if key in seen:
            raise PatientOpeningDataConflict(
                "This patient has multiple opening Pulse balances for the same device, so they cannot be corrected automatically."
            )
        seen.add(key)
    return packs


def _package_usage_rows(
    db: Session,
    *,
    workspace_id: UUID,
    package_id: UUID,
    for_update: bool,
) -> list[PackageUsage]:
    stmt = select(PackageUsage).where(
        PackageUsage.workspace_id == workspace_id,
        PackageUsage.patient_package_id == package_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt).all())


def _pulse_usage_rows(
    db: Session,
    *,
    workspace_id: UUID,
    pack_id: UUID,
    for_update: bool,
) -> list[PulseUsage]:
    stmt = select(PulseUsage).where(
        PulseUsage.workspace_id == workspace_id,
        PulseUsage.patient_pulse_pack_id == pack_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt).all())


def _linked_transactions(
    db: Session,
    *,
    workspace_id: UUID,
    package: PatientPackage | None = None,
    pulse_pack: PatientPulsePack | None = None,
    for_update: bool,
) -> list[PaymentTransaction]:
    conditions = []
    purchase_id: UUID | None = None
    if package is not None:
        conditions.append(PaymentTransaction.patient_package_id == package.id)
        purchase_id = package.purchase_transaction_id
    if pulse_pack is not None:
        conditions.append(PaymentTransaction.patient_pulse_pack_id == pulse_pack.id)
        purchase_id = pulse_pack.purchase_transaction_id
    if purchase_id is not None:
        conditions.extend(
            [
                PaymentTransaction.id == purchase_id,
                PaymentTransaction.reference_transaction_id == purchase_id,
            ]
        )
    if not conditions:
        return []
    stmt = select(PaymentTransaction).where(
        PaymentTransaction.workspace_id == workspace_id,
        or_(*conditions),
    )
    if for_update:
        stmt = stmt.with_for_update()
    rows = list(db.scalars(stmt).all())
    ids = [row.id for row in rows]
    if ids:
        refs = select(PaymentTransaction).where(
            PaymentTransaction.workspace_id == workspace_id,
            PaymentTransaction.reference_transaction_id.in_(ids),
        )
        if for_update:
            refs = refs.with_for_update()
        by_id = {row.id: row for row in rows}
        by_id.update({row.id: row for row in db.scalars(refs).all()})
        rows = list(by_id.values())
    return rows


def _package_identity_replaceable(
    db: Session,
    *,
    workspace_id: UUID,
    package: PatientPackage,
    usage_rows: list[PackageUsage] | None = None,
    for_update: bool = False,
) -> bool:
    usages = usage_rows if usage_rows is not None else _package_usage_rows(
        db, workspace_id=workspace_id, package_id=package.id, for_update=for_update
    )
    if usages or package.purchase_transaction_id is not None or package.origin_appointment_id is not None:
        return False
    if package.external_id is not None or package.source == "integration":
        return False
    appointment_stmt = select(Appointment.id).where(
        Appointment.workspace_id == workspace_id,
        Appointment.patient_package_id == package.id,
    ).limit(1)
    additional_stmt = select(AppointmentAdditionalService.id).where(
        AppointmentAdditionalService.workspace_id == workspace_id,
        AppointmentAdditionalService.patient_package_id == package.id,
    ).limit(1)
    if for_update:
        appointment_stmt = appointment_stmt.with_for_update()
        additional_stmt = additional_stmt.with_for_update()
    if db.scalar(appointment_stmt) is not None or db.scalar(additional_stmt) is not None:
        return False
    return not _linked_transactions(
        db, workspace_id=workspace_id, package=package, for_update=for_update
    )


def _pulse_identity_replaceable(
    db: Session,
    *,
    workspace_id: UUID,
    pack: PatientPulsePack,
    usage_rows: list[PulseUsage] | None = None,
    for_update: bool = False,
) -> bool:
    usages = usage_rows if usage_rows is not None else _pulse_usage_rows(
        db, workspace_id=workspace_id, pack_id=pack.id, for_update=for_update
    )
    if usages:
        return False
    additional_stmt = select(AppointmentAdditionalService.id).where(
        AppointmentAdditionalService.workspace_id == workspace_id,
        AppointmentAdditionalService.pulse_resolution_pulse_pack_id == pack.id,
    ).limit(1)
    if for_update:
        additional_stmt = additional_stmt.with_for_update()
    settlement_stmt = select(AppointmentPulseSettlement.id).where(
        AppointmentPulseSettlement.workspace_id == workspace_id,
        AppointmentPulseSettlement.resolution_pulse_pack_id == pack.id,
    ).limit(1)
    if for_update:
        settlement_stmt = settlement_stmt.with_for_update()
    if db.scalar(additional_stmt) is not None or db.scalar(settlement_stmt) is not None:
        return False
    return not _linked_transactions(
        db, workspace_id=workspace_id, pulse_pack=pack, for_update=for_update
    )


def _package_read(
    db: Session,
    *,
    workspace_id: UUID,
    package: PatientPackage,
) -> PatientOpeningPackageRead:
    read = package_read(db, package, include_financials=False)
    return PatientOpeningPackageRead(
        package_id=package.id,
        offer_id=package.package_offer_id,
        service_id=package.service_id,
        name=package.name,
        sessions_total=int(package.sessions_purchased),
        opening_sessions_remaining=int(package.opening_sessions_remaining or 0),
        current_remaining=int(read.sessions_remaining),
        sessions_reserved=int(read.sessions_reserved),
        sessions_consumed=int(read.sessions_consumed),
        replaceable_identity=_package_identity_replaceable(
            db, workspace_id=workspace_id, package=package
        ),
    )


def _pulse_read(
    db: Session,
    *,
    workspace_id: UUID,
    pack: PatientPulsePack,
) -> PatientOpeningPulseRead:
    usages = _pulse_usage_rows(db, workspace_id=workspace_id, pack_id=pack.id, for_update=False)
    consumed = sum(int(row.pulses_used) for row in usages if row.status == "consumed")
    opening = int(pack.pulses_purchased)
    return PatientOpeningPulseRead(
        opening_pack_id=pack.id,
        device_key=pack.device_key,
        device_name=pack.device_name,
        opening_pulses=opening,
        current_remaining=max(opening - consumed, 0),
        pulses_consumed=consumed,
        replaceable_identity=_pulse_identity_replaceable(
            db, workspace_id=workspace_id, pack=pack, usage_rows=usages
        ),
    )


def get_patient_opening_data(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
) -> PatientOpeningDataRead:
    patient = db.scalar(
        select(Patient).where(
            Patient.workspace_id == workspace_id,
            Patient.id == patient_id,
        )
    )
    if patient is None:
        raise PatientOpeningDataNotFound("Patient not found.")
    packages = _opening_packages(
        db, workspace_id=workspace_id, patient_id=patient_id, for_update=False
    )
    pulse_packs = _opening_pulse_packs(
        db, workspace_id=workspace_id, patient_id=patient_id, for_update=False
    )
    return PatientOpeningDataRead(
        patient_id=patient.id,
        customer_id=patient.customer_id,
        packages=[
            _package_read(db, workspace_id=workspace_id, package=package)
            for package in packages
        ],
        pulse_balances=[
            _pulse_read(db, workspace_id=workspace_id, pack=pack) for pack in pulse_packs
        ],
    )


def update_patient_opening_data(
    db: Session,
    *,
    workspace_id: UUID,
    patient_id: UUID,
    payload: PatientOpeningDataUpdate,
    actor_user_id: UUID | None,
) -> PatientOpeningDataRead:
    patient = db.scalar(
        select(Patient)
        .where(Patient.workspace_id == workspace_id, Patient.id == patient_id)
        .with_for_update()
    )
    if patient is None:
        raise PatientOpeningDataNotFound("Patient not found.")

    customer_id_changed = payload.customer_id != patient.customer_id
    if customer_id_changed:
        require_tia_patient_fields_writable(
            db,
            workspace_id=workspace_id,
            patient_id=patient.id,
            fields={"customer_id"},
        )

    current_packages = _opening_packages(
        db, workspace_id=workspace_id, patient_id=patient_id, for_update=True
    )
    current_by_id = {package.id: package for package in current_packages}
    requested_existing_ids = {
        item.package_id for item in payload.packages if item.package_id is not None
    }
    unknown_ids = requested_existing_ids - set(current_by_id)
    if unknown_ids:
        raise PatientOpeningDataNotFound("Opening package not found for this patient.")

    usage_by_package_id: dict[UUID, list[PackageUsage]] = {}
    totals_by_package_id: dict[UUID, tuple[int, int]] = {}
    for package in current_packages:
        usages = _package_usage_rows(
            db, workspace_id=workspace_id, package_id=package.id, for_update=True
        )
        usage_by_package_id[package.id] = usages
        reserved = sum(int(row.sessions_used) for row in usages if row.status == "reserved")
        consumed = sum(int(row.sessions_used) for row in usages if row.status == "consumed")
        totals_by_package_id[package.id] = (reserved, consumed)

    package_plans: list[
        tuple[PatientOpeningPackageUpdate, PatientPackage | None, ServicePackageOffer | None, bool]
    ] = []
    effective_offer_ids: set[UUID] = set()
    for item in payload.packages:
        current = current_by_id.get(item.package_id) if item.package_id is not None else None
        desired_offer: ServicePackageOffer | None = None
        replacing = False
        if current is not None and (
            item.offer_id is None or item.offer_id == current.package_offer_id
        ):
            reserved, consumed = totals_by_package_id[current.id]
            if item.sessions_remaining > int(current.sessions_purchased):
                raise PatientOpeningDataConflict(
                    "Opening package balance cannot exceed the package total session count."
                )
            if item.sessions_remaining < reserved + consumed:
                raise PatientOpeningDataConflict(
                    "Opening package balance cannot be lower than the sessions already reserved or consumed."
                )
            effective_offer_id = current.package_offer_id
        else:
            if item.offer_id is None:
                raise PatientOpeningDataConflict(
                    "Choose a package offer before adding or replacing an opening package."
                )
            desired_offer = db.scalar(
                select(ServicePackageOffer)
                .where(
                    ServicePackageOffer.workspace_id == workspace_id,
                    ServicePackageOffer.id == item.offer_id,
                    ServicePackageOffer.is_active.is_(True),
                )
                .with_for_update()
            )
            if desired_offer is None:
                raise PatientOpeningDataNotFound("Active package offer not found.")
            if item.sessions_remaining > int(desired_offer.sessions_count):
                raise PatientOpeningDataConflict(
                    "Opening package balance cannot exceed the selected package total session count."
                )
            replacing = current is not None
            if current is not None and not _package_identity_replaceable(
                db,
                workspace_id=workspace_id,
                package=current,
                usage_rows=usage_by_package_id[current.id],
                for_update=True,
            ):
                raise PatientOpeningDataConflict(
                    "لا يمكن استبدال هذه الباكدج لأن عليها استخدام أو تاريخ مالي مسجل بالفعل. يمكن تعديل الرصيد الافتتاحي فقط بما يتوافق مع الاستخدام الحالي."
                )
            effective_offer_id = desired_offer.id

        if effective_offer_id is not None:
            if effective_offer_id in effective_offer_ids:
                raise PatientOpeningDataConflict(
                    "لا يمكن تكرار نفس عرض الباكدج ضمن الأرصدة الافتتاحية."
                )
            effective_offer_ids.add(effective_offer_id)
        package_plans.append((item, current, desired_offer, replacing))

    removed_packages = [
        package for package in current_packages if package.id not in requested_existing_ids
    ]
    for package in removed_packages:
        if not _package_identity_replaceable(
            db,
            workspace_id=workspace_id,
            package=package,
            usage_rows=usage_by_package_id[package.id],
            for_update=True,
        ):
            raise PatientOpeningDataConflict(
                "لا يمكن حذف هذه الباكدج لأن عليها استخدام أو تاريخ مالي مسجل بالفعل."
            )

    current_pulse_packs = _opening_pulse_packs(
        db, workspace_id=workspace_id, patient_id=patient_id, for_update=True
    )
    current_by_key = {pack.device_key: pack for pack in current_pulse_packs}
    requested_by_key = {item.device_key: item for item in payload.pulse_balances}
    pulse_usage_by_key: dict[str, list[PulseUsage]] = {}

    for key, pack in current_by_key.items():
        usages = _pulse_usage_rows(
            db, workspace_id=workspace_id, pack_id=pack.id, for_update=True
        )
        pulse_usage_by_key[key] = usages
        requested = requested_by_key.get(key)
        if requested is not None:
            consumed_pulses = sum(
                int(row.pulses_used) for row in usages if row.status == "consumed"
            )
            if requested.pulses_remaining < consumed_pulses:
                raise PatientOpeningDataConflict(
                    "Opening Pulse balance cannot be lower than the pulses already consumed."
                )
        elif not _pulse_identity_replaceable(
            db,
            workspace_id=workspace_id,
            pack=pack,
            usage_rows=usages,
            for_update=True,
        ):
            raise PatientOpeningDataConflict(
                "لا يمكن تغيير جهاز رصيد الـPulses لأن الرصيد تم استخدامه أو ربطه بتاريخ تشغيلي بالفعل."
            )

    new_device_keys = set(requested_by_key) - set(current_by_key)
    if new_device_keys:
        active_devices = {
            row.device_key: row
            for row in db.scalars(
                select(ClinicLaserDevice)
                .where(
                    ClinicLaserDevice.workspace_id == workspace_id,
                    ClinicLaserDevice.device_key.in_(new_device_keys),
                    ClinicLaserDevice.is_active.is_(True),
                )
                .with_for_update()
            ).all()
        }
        missing = sorted(new_device_keys - set(active_devices))
        if missing:
            raise PatientOpeningDataNotFound("Active laser device not found.")

    old_customer_id = patient.customer_id
    patient.customer_id = payload.customer_id
    if old_customer_id != patient.customer_id:
        record_activity_event(
            db,
            workspace_id=workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="patient.opening_data_updated",
            entity_type="patient",
            entity_id=patient.id,
            summary="Patient opening data updated",
            metadata={"customer_id_changed": True},
        )

    replacing_ids = {
        current.id
        for _item, current, _offer, replacing in package_plans
        if replacing and current is not None
    }
    for package in removed_packages:
        record_activity_event(
            db,
            workspace_id=workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="package.opening_balance_corrected",
            entity_type="patient_package",
            entity_id=package.id,
            summary="Opening package removed",
            metadata={
                "old_offer_id": str(package.package_offer_id) if package.package_offer_id else None,
                "old_remaining": int(package.opening_sessions_remaining or 0),
            },
        )
        db.delete(package)
    for package_id in replacing_ids:
        db.delete(current_by_id[package_id])
    if removed_packages or replacing_ids:
        db.flush()

    for item, current, desired_offer, replacing in package_plans:
        if current is not None and not replacing:
            old_remaining = int(current.opening_sessions_remaining or 0)
            current.opening_sessions_remaining = item.sessions_remaining
            if old_remaining != item.sessions_remaining:
                record_activity_event(
                    db,
                    workspace_id=workspace_id,
                    actor_type="staff",
                    actor_user_id=actor_user_id,
                    action="package.opening_balance_corrected",
                    entity_type="patient_package",
                    entity_id=current.id,
                    summary="Opening package balance corrected",
                    metadata={
                        "old_remaining": old_remaining,
                        "new_remaining": item.sessions_remaining,
                        "offer_id": str(current.package_offer_id) if current.package_offer_id else None,
                    },
                )
            continue

        assert desired_offer is not None
        old_offer_id = current.package_offer_id if current is not None else None
        try:
            new_package = create_patient_package_opening_balance(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                offer_id=desired_offer.id,
                sessions_remaining=item.sessions_remaining,
                amount_paid_minor=0,
                payment_method="unknown",
                created_by_user_id=actor_user_id,
            )
        except (PackageOfferError, PackageOfferNotFound) as exc:
            raise PatientOpeningDataConflict(str(exc)) from exc
        record_activity_event(
            db,
            workspace_id=workspace_id,
            actor_type="staff",
            actor_user_id=actor_user_id,
            action="package.opening_balance_corrected",
            entity_type="patient_package",
            entity_id=new_package.id,
            summary=("Opening package identity corrected" if replacing else "Opening package added"),
            metadata={
                "old_offer_id": str(old_offer_id) if old_offer_id else None,
                "new_offer_id": str(new_package.package_offer_id) if new_package.package_offer_id else None,
                "new_remaining": item.sessions_remaining,
            },
        )

    for key, pack in current_by_key.items():
        requested = requested_by_key.get(key)
        if requested is None:
            record_activity_event(
                db,
                workspace_id=workspace_id,
                actor_type="staff",
                actor_user_id=actor_user_id,
                action="pulse_pack.opening_balance_corrected",
                entity_type="patient_pulse_pack",
                entity_id=pack.id,
                summary="Opening Pulse balance removed",
                metadata={"device_key": key, "old_pulses": int(pack.pulses_purchased)},
            )
            db.delete(pack)
            continue
        old_pulses = int(pack.pulses_purchased)
        pack.pulses_purchased = requested.pulses_remaining
        if old_pulses != requested.pulses_remaining:
            record_activity_event(
                db,
                workspace_id=workspace_id,
                actor_type="staff",
                actor_user_id=actor_user_id,
                action="pulse_pack.opening_balance_corrected",
                entity_type="patient_pulse_pack",
                entity_id=pack.id,
                summary="Opening Pulse balance corrected",
                metadata={
                    "device_key": key,
                    "old_pulses": old_pulses,
                    "new_pulses": requested.pulses_remaining,
                },
            )

    for key in sorted(new_device_keys):
        requested = requested_by_key[key]
        try:
            create_patient_pulse_opening_balance(
                db,
                workspace_id=workspace_id,
                patient_id=patient.id,
                device_key=key,
                pulses_remaining=requested.pulses_remaining,
                created_by_user_id=actor_user_id,
            )
        except (PulseBillingError, PulseBillingNotFound) as exc:
            raise PatientOpeningDataConflict(str(exc)) from exc

    db.flush()
    return get_patient_opening_data(
        db,
        workspace_id=workspace_id,
        patient_id=patient.id,
    )
