from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.api.routes import clinic as clinic_route
from app.database.base import Base
from app.models.clinic_inventory import ClinicLaserDevice, ServiceDevicePrice
from app.models.service import Service
from app.schemas.clinic import ServiceCreate, ServiceDevicePriceInput, ServiceUpdate


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Service.__table__,
            ClinicLaserDevice.__table__,
            ServiceDevicePrice.__table__,
        ],
    )
    return engine, Session(engine)


def _patch_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(clinic_route, "sync_service_doctor_assignments", lambda *args, **kwargs: None)
    monkeypatch.setattr(clinic_route, "record_activity_event", lambda *args, **kwargs: None)


def test_service_create_is_single_transaction_and_category_wins(monkeypatch) -> None:
    _patch_side_effects(monkeypatch)
    engine, db = _session()
    workspace = SimpleNamespace(id=uuid4())
    actor_id = uuid4()
    try:
        db.add(
            ClinicLaserDevice(
                workspace_id=workspace.id,
                device_key="candela",
                name="Candela Gentle",
                is_active=True,
            )
        )
        db.commit()

        for index in (1, 2):
            service = clinic_route.create_service(
                payload=ServiceCreate(
                    name=f"Laser {index}",
                    slug=f"laser-{index}",
                    operational_category="laser",
                    duration_minutes=60,
                    price_minor=0,
                    requires_laser_device=False,
                    device_prices=[
                        ServiceDevicePriceInput(
                            device_key="candela",
                            price_minor=250_000 + index,
                            duration_minutes=60,
                        )
                    ],
                ),
                workspace=workspace,
                actor_user_id=actor_id,
                db=db,
            )
            assert service.requires_laser_device is True

        assert db.scalar(select(func.count()).select_from(Service)) == 2
        assert db.scalar(select(func.count()).select_from(ServiceDevicePrice)) == 2
    finally:
        db.close()
        engine.dispose()


def test_service_edit_both_directions_derives_flag_and_deactivates_stale_prices(monkeypatch) -> None:
    _patch_side_effects(monkeypatch)
    engine, db = _session()
    workspace = SimpleNamespace(id=uuid4())
    actor_id = uuid4()
    try:
        db.add(
            ClinicLaserDevice(
                workspace_id=workspace.id,
                device_key="prime",
                name="Prime Lase",
                is_active=True,
            )
        )
        db.commit()
        service = clinic_route.create_service(
            payload=ServiceCreate(
                name="Switchable",
                slug="switchable",
                operational_category="dermatology",
                duration_minutes=45,
                price_minor=100_000,
                requires_laser_device=True,
            ),
            workspace=workspace,
            actor_user_id=actor_id,
            db=db,
        )
        assert service.requires_laser_device is False

        service = clinic_route.update_service(
            service_id=service.id,
            payload=ServiceUpdate(
                operational_category="laser",
                requires_laser_device=False,
                device_prices=[
                    ServiceDevicePriceInput(
                        device_key="prime",
                        price_minor=220_000,
                        duration_minutes=50,
                    )
                ],
            ),
            workspace=workspace,
            actor_user_id=actor_id,
            db=db,
        )
        assert service.operational_category == "laser"
        assert service.requires_laser_device is True
        price = db.scalar(
            select(ServiceDevicePrice).where(ServiceDevicePrice.service_id == service.id)
        )
        assert price is not None and price.is_active is True

        service = clinic_route.update_service(
            service_id=service.id,
            payload=ServiceUpdate(
                operational_category="dermatology",
                requires_laser_device=True,
            ),
            workspace=workspace,
            actor_user_id=actor_id,
            db=db,
        )
        assert service.operational_category == "dermatology"
        assert service.requires_laser_device is False
        db.refresh(price)
        assert price.is_active is False
    finally:
        db.close()
        engine.dispose()


def test_service_create_rolls_back_service_when_device_configuration_fails(monkeypatch) -> None:
    _patch_side_effects(monkeypatch)
    engine, db = _session()
    workspace = SimpleNamespace(id=uuid4())
    try:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            clinic_route.create_service(
                payload=ServiceCreate(
                    name="Should Roll Back",
                    slug="should-roll-back",
                    operational_category="laser",
                    duration_minutes=60,
                    price_minor=0,
                    device_prices=[
                        ServiceDevicePriceInput(
                            device_key="missing-device",
                            price_minor=250_000,
                            duration_minutes=60,
                        )
                    ],
                ),
                workspace=workspace,
                actor_user_id=uuid4(),
                db=db,
            )

        assert exc_info.value.status_code == 422
        assert db.scalar(select(func.count()).select_from(Service)) == 0
        assert db.scalar(select(func.count()).select_from(ServiceDevicePrice)) == 0
    finally:
        db.close()
        engine.dispose()
