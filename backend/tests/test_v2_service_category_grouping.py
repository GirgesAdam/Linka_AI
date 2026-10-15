from __future__ import annotations

from app.services.agent_v2.multi_service_booking import (
    partition_service_ids_by_operational_category,
    service_operational_category,
)


def _catalog():
    return {
        "services": [
            {"id": "laser-a", "name": "A", "category": "free", "operational_category": "laser", "requires_laser_device": True},
            {"id": "laser-b", "name": "B", "category": "different wording", "operational_category": "laser", "requires_laser_device": True},
            {"id": "derm-c", "name": "C", "category": "laser-looking", "operational_category": "dermatology", "requires_laser_device": False},
            {"id": "unknown-d", "name": "Laser by name only", "category": "laser", "requires_laser_device": True},
            {"id": "unknown-e", "name": "Another", "category": "laser", "requires_laser_device": True},
        ]
    }


def test_operational_category_is_only_grouping_authority() -> None:
    catalog = _catalog()
    assert service_operational_category(catalog, "laser-a") == "laser"
    assert service_operational_category(catalog, "derm-c") == "dermatology"
    assert service_operational_category(catalog, "unknown-d") is None


def test_partition_preserves_peer_order_with_same_category_grouped() -> None:
    assert partition_service_ids_by_operational_category(
        _catalog(), ["laser-a", "laser-b", "derm-c"]
    ) == [("laser", ["laser-a", "laser-b"]), ("dermatology", ["derm-c"])]


def test_unknown_categories_fail_closed_as_independent_singletons() -> None:
    assert partition_service_ids_by_operational_category(
        _catalog(), ["unknown-d", "unknown-e"]
    ) == [(None, ["unknown-d"]), (None, ["unknown-e"])]


def test_free_form_category_and_requires_laser_do_not_override_canonical_category() -> None:
    assert partition_service_ids_by_operational_category(
        _catalog(), ["laser-a", "derm-c", "unknown-d"]
    ) == [
        ("laser", ["laser-a"]),
        ("dermatology", ["derm-c"]),
        (None, ["unknown-d"]),
    ]
