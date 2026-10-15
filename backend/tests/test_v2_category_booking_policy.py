from __future__ import annotations

from app.services.agent_v2.multi_service_booking import (
    category_group_duration_minutes,
    common_doctor_ids,
    common_laser_device_keys,
    effective_service_duration_minutes,
    select_main_service_id,
)


def _device(key: str, duration: int):
    return {
        "device_key": key,
        "configured": True,
        "price_minor": 10000,
        "duration_minutes": duration,
    }


def _catalog():
    return {
        "services": [
            {"id": "a", "duration_minutes": 20, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("candela", 15), _device("deka", 25)]},
            {"id": "b", "duration_minutes": 30, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("candela", 30)]},
            {"id": "c", "duration_minutes": 40, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("candela", 40)]},
            {"id": "d", "duration_minutes": 45, "operational_category": "dermatology", "requires_laser_device": False},
        ],
        "doctors": [
            {"id": "doc-common", "service_ids": ["a", "b", "c", "d"], "branch_ids": ["branch-1"]},
            {"id": "doc-a", "service_ids": ["a"], "branch_ids": ["branch-1"]},
            {"id": "doc-b", "service_ids": ["b"], "branch_ids": ["branch-1"]},
        ],
    }


def test_same_category_two_services_use_max_duration_not_sum() -> None:
    catalog = _catalog()
    assert category_group_duration_minutes(catalog, ["a", "b"], device_key="candela") == 30
    assert select_main_service_id(catalog, ["a", "b"], device_key="candela") == "b"


def test_three_same_category_services_use_max_effective_duration() -> None:
    catalog = _catalog()
    assert category_group_duration_minutes(catalog, ["a", "b", "c"], device_key="candela") == 40


def test_laser_duration_comes_from_selected_device_not_base_duration() -> None:
    catalog = {
        "services": [
            {"id": "a", "duration_minutes": 90, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("candela", 20)]},
            {"id": "b", "duration_minutes": 10, "operational_category": "laser", "requires_laser_device": True, "laser_devices": [_device("candela", 45)]},
        ]
    }
    assert effective_service_duration_minutes(catalog, "a", device_key="candela") == 20
    assert effective_service_duration_minutes(catalog, "b", device_key="candela") == 45
    assert category_group_duration_minutes(catalog, ["a", "b"], device_key="candela") == 45
    assert select_main_service_id(catalog, ["a", "b"], device_key="candela") == "b"


def test_same_category_common_doctor_is_intersection() -> None:
    assert common_doctor_ids(_catalog(), ["a", "b"], branch_id="branch-1") == ["doc-common"]


def test_no_common_doctor_returns_empty_without_fallback() -> None:
    catalog = _catalog()
    catalog["doctors"] = [
        {"id": "doc-a", "service_ids": ["a"], "branch_ids": ["branch-1"]},
        {"id": "doc-b", "service_ids": ["b"], "branch_ids": ["branch-1"]},
    ]
    assert common_doctor_ids(catalog, ["a", "b"], branch_id="branch-1") == []


def test_laser_common_device_uses_only_configured_intersection() -> None:
    assert common_laser_device_keys(_catalog(), ["a", "b"]) == ["candela"]


def test_no_common_laser_device_returns_empty() -> None:
    catalog = _catalog()
    catalog["services"][1]["laser_devices"] = [_device("alex", 30)]
    assert common_laser_device_keys(catalog, ["a", "b"]) == []

