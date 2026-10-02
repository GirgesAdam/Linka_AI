from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.inventory import ClinicProductCreate, InventoryItemCreate, InventoryUsageCreate
from app.services.finance_dashboard import _financial_period_buckets, financial_dashboard_trend
from app.services.inventory import (
    InventoryOperationError,
    add_appointment_product,
    create_clinic_laser_device,
    delete_appointment_product,
    is_laser_service,
    list_clinic_laser_devices,
    update_clinic_laser_device,
)


def test_laser_device_pricing_uses_explicit_service_flag_not_name() -> None:
    assert is_laser_service(SimpleNamespace(name="Full Body", category="Other", requires_laser_device=True))
    assert not is_laser_service(SimpleNamespace(name="Laser sounding name", category="Laser", requires_laser_device=False))


class _LaserDeviceDb:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.scalar_value = None

    def scalars(self, _stmt):
        return iter(self.rows)

    def scalar(self, _stmt):
        return self.scalar_value

    def add_all(self, rows):
        self.rows.extend(rows)

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        now = datetime(2026, 10, 2, tzinfo=UTC)
        for row in self.rows:
            if getattr(row, "id", None) is None:
                row.id = uuid4()
            if getattr(row, "created_at", None) is None:
                row.created_at = now
            if getattr(row, "updated_at", None) is None:
                row.updated_at = now


def test_dynamic_laser_device_registry_backfills_legacy_devices_and_adds_custom_device() -> None:
    workspace_id = uuid4()
    db = _LaserDeviceDb()

    initial = list_clinic_laser_devices(db, workspace_id=workspace_id)
    assert {(item.device_key, item.name) for item in initial} == {
        ("prime_lase", "Prime Lase"),
        ("candela_gentle", "Candela Gentle"),
    }

    added = create_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        name="  DEKA   Again  ",
    )
    assert added.name == "DEKA Again"
    assert added.device_key.startswith("device_")
    assert added.device_key not in {"prime_lase", "candela_gentle"}
    assert added.is_active is True


def test_dynamic_laser_device_registry_can_disable_and_reactivate_without_changing_key() -> None:
    workspace_id = uuid4()
    db = _LaserDeviceDb()
    device = create_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        name="DEKA Again",
    )
    row = next(item for item in db.rows if item.device_key == device.device_key)

    db.scalar_value = row
    disabled = update_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        device_id=row.id,
        name=None,
        is_active=False,
    )
    assert disabled.is_active is False
    original_key = disabled.device_key

    db.scalar_value = row
    reactivated = update_clinic_laser_device(
        db,
        workspace_id=workspace_id,
        device_id=row.id,
        name="DEKA Again Pro",
        is_active=True,
    )
    assert reactivated.is_active is True
    assert reactivated.name == "DEKA Again Pro"
    assert reactivated.device_key == original_key


def test_dynamic_laser_device_registry_rejects_duplicate_name_on_rename() -> None:
    workspace_id = uuid4()
    db = _LaserDeviceDb()
    first = create_clinic_laser_device(db, workspace_id=workspace_id, name="DEKA Again")
    second = create_clinic_laser_device(db, workspace_id=workspace_id, name="Soprano Titanium")
    first_row = next(item for item in db.rows if item.device_key == first.device_key)
    second_row = next(item for item in db.rows if item.device_key == second.device_key)
    db.scalar_value = second_row

    with pytest.raises(InventoryOperationError, match="already exists"):
        update_clinic_laser_device(
            db,
            workspace_id=workspace_id,
            device_id=second_row.id,
            name=first_row.name.lower(),
            is_active=None,
        )


def test_new_inventory_contract_is_ml_only_and_products_have_stock_quantity() -> None:
    item = InventoryItemCreate(name="Injectable", quantity_ml="12.5")
    usage = InventoryUsageCreate(used_ml="0.75")
    product = ClinicProductCreate(name="Skin Protector", quantity_on_hand=18)

    assert item.model_dump() == {
        "name": "Injectable",
        "quantity_ml": item.quantity_ml,
        "low_stock_threshold_ml": None,
        "notes": None,
    }
    assert usage.model_dump() == {"used_ml": usage.used_ml, "note": None}
    assert product.quantity_on_hand == 18
    assert "concentration_mg_per_ml" not in InventoryItemCreate.model_fields
    assert "appointment_id" not in InventoryUsageCreate.model_fields


def test_product_sale_decrements_stock_and_removal_restores_it() -> None:
    workspace_id = uuid4()
    appointment_id = uuid4()
    product_id = uuid4()
    line_id = uuid4()
    appointment = SimpleNamespace(
        id=appointment_id,
        workspace_id=workspace_id,
        status="confirmed",
        currency="EGP",
    )
    product = SimpleNamespace(
        id=product_id,
        workspace_id=workspace_id,
        name="Skin Protector",
        quantity_on_hand=5,
        is_active=True,
    )

    class _Db:
        def __init__(self):
            self.scalar_values = [appointment, product]
            self.added = None
            self.deleted = None

        def scalar(self, _stmt):
            return self.scalar_values.pop(0)

        def add(self, value):
            self.added = value
            value.id = line_id

        def delete(self, value):
            self.deleted = value

        def flush(self):
            return None

    db = _Db()
    line = add_appointment_product(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        product_id=product_id,
        quantity=2,
        unit_price_minor=15000,
        created_by_user_id=None,
    )
    assert product.quantity_on_hand == 3
    assert line.quantity == 2

    db.scalar_values = [line, product]
    delete_appointment_product(
        db,
        workspace_id=workspace_id,
        appointment_id=appointment_id,
        line_id=line_id,
    )
    assert product.quantity_on_hand == 5
    assert db.deleted is line


def test_month_dashboard_has_four_full_calendar_buckets() -> None:
    buckets = _financial_period_buckets(
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        mode="month",
    )
    assert [(start.day, end.day) for _, start, end in buckets] == [(1, 7), (8, 14), (15, 21), (22, 30)]


def test_year_dashboard_has_twelve_calendar_month_buckets() -> None:
    buckets = _financial_period_buckets(
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        mode="year",
    )
    assert len(buckets) == 12
    assert buckets[0][1:] == (date(2026, 1, 1), date(2026, 1, 31))
    assert buckets[-1][1:] == (date(2026, 12, 1), date(2026, 12, 31))


def test_dashboard_finance_uses_cairo_local_date_and_same_profit_formula() -> None:
    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class _Db:
        def __init__(self):
            self.calls = 0

        def execute(self, _stmt):
            self.calls += 1
            if self.calls == 1:
                return _Result([
                    # 2026-09-01 01:30 Cairo: must land in September week 1.
                    (datetime(2026, 8, 31, 22, 30, tzinfo=UTC), "payment", 10000),
                    (datetime(2026, 9, 8, 9, 0, tzinfo=UTC), "refund", 2000),
                ])
            return _Result([(date(2026, 9, 22), 3000)])

    result = financial_dashboard_trend(
        _Db(),
        workspace_id=uuid4(),
        timezone_name="Africa/Cairo",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        mode="month",
        currency="EGP",
    )

    assert len(result.points) == 4
    assert result.points[0].gross_payments_minor == 10000
    assert result.points[1].refunds_minor == 2000
    assert result.points[3].expenses_minor == 3000
    assert sum(point.net_revenue_minor for point in result.points) == 8000
    assert sum(point.profit_minor for point in result.points) == 5000
