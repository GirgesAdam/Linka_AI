from datetime import time

import pytest
from pydantic import ValidationError

from app.schemas.clinic import ServiceCreate, ServiceUpdate, WorkingHourInterval


def test_service_rejects_float_style_negative_price() -> None:
    with pytest.raises(ValidationError):
        ServiceCreate(name="Laser", slug="laser", duration_minutes=30, price_minor=-1)


def test_working_hours_require_end_after_start() -> None:
    with pytest.raises(ValidationError):
        WorkingHourInterval(weekday=0, start_time=time(18, 0), end_time=time(17, 0))


def test_working_hours_reject_overlap() -> None:
    from app.schemas.clinic import WorkingHoursReplace

    with pytest.raises(ValidationError):
        WorkingHoursReplace(
            intervals=[
                WorkingHourInterval(weekday=0, start_time=time(10, 0), end_time=time(14, 0)),
                WorkingHourInterval(weekday=0, start_time=time(13, 0), end_time=time(18, 0)),
            ]
        )


def test_service_keeps_legacy_category_and_derives_operational_category() -> None:
    service = ServiceCreate(
        name="Hydrafacial",
        slug="hydrafacial",
        category="Facial",
        duration_minutes=45,
        price_minor=180_000,
    )
    assert service.category == "Facial"
    assert service.operational_category == "dermatology"
    assert service.requires_laser_device is False


def test_service_operational_category_is_laser_requirement_authority() -> None:
    laser = ServiceCreate(
        name="Full Body",
        slug="full-body",
        operational_category="laser",
        duration_minutes=60,
        price_minor=0,
        requires_laser_device=False,
    )
    assert laser.operational_category == "laser"
    assert laser.requires_laser_device is True

    dermatology = ServiceCreate(
        name="Facial",
        slug="facial",
        operational_category="dermatology",
        duration_minutes=45,
        price_minor=100_000,
        requires_laser_device=True,
    )
    assert dermatology.operational_category == "dermatology"
    assert dermatology.requires_laser_device is False


def test_service_update_operational_category_wins_over_contradictory_flag() -> None:
    to_laser = ServiceUpdate(
        operational_category="laser",
        requires_laser_device=False,
    )
    assert to_laser.requires_laser_device is True

    from_laser = ServiceUpdate(
        operational_category="dermatology",
        requires_laser_device=True,
    )
    assert from_laser.requires_laser_device is False

    unrelated_update = ServiceUpdate(name="Renamed")
    assert unrelated_update.operational_category is None
    assert unrelated_update.requires_laser_device is None
