from app.agents.v2.semantic_context import build_semantic_context


def test_raw_active_task_and_pending_choice_are_never_copied_to_model_input() -> None:
    canonical_service_id = "3a672050-8f8f-49b1-942a-very-internal-service-id"
    raw_active_task = {
        "constraints": {"service_id": canonical_service_id},
        "write_authorization": {
            "authorized": True,
            "operation": "booking",
            "source_turn_id": "private-turn-id",
        },
    }
    raw_pending_choice = {
        "purpose": "booking_slot",
        "option_snapshot_id": "internal-snapshot-id",
        "options": [{"appointment_id": "private-appointment-id"}],
    }

    context = build_semantic_context(
        {
            "services": [{"id": canonical_service_id, "name": "ليزر إبط"}],
            "doctors": [],
            "appointments": [],
            "packages": [],
        },
        active_task=raw_active_task,
        pending_choice=raw_pending_choice,
    )

    assert context.model_input["active_task"] == {}
    assert context.model_input["pending_choice"] == {}
    encoded = repr(context.model_input)
    assert canonical_service_id not in encoded
    assert "private-turn-id" not in encoded
    assert "internal-snapshot-id" not in encoded
    assert "private-appointment-id" not in encoded


def test_semantic_catalog_keeps_business_constraints_server_side() -> None:
    context = build_semantic_context(
        {
            "services": [
                {
                    "id": "service-laser",
                    "name": "ليزر إبط",
                    "category": "laser",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {
                            "device_key": "candela_gentle",
                            "device_name": "Candela Gentle",
                        }
                    ],
                }
            ],
            "doctors": [
                {
                    "id": "doctor-1",
                    "name": "مريم",
                    "specialization": "Dermatology",
                    "service_ids": ["service-laser"],
                }
            ],
            "appointments": [],
            "packages": [],
        }
    )

    service = context.model_input["services"][0]
    doctor = context.model_input["doctors"][0]

    assert service == {"ref": "S1", "name": "ليزر إبط"}
    assert context.model_input["devices"] == [
        {"ref": "V1", "name": "Candela Gentle"}
    ]
    assert context.server_metadata["focus_details"]["S1"]["category"] == "laser"
    assert context.server_metadata["focus_details"]["S1"]["device_refs"] == ["V1"]
    assert "requires_laser_device" not in service
    assert "service_refs" not in doctor
    assert context.reference_map["S1"].metadata["requires_laser_device"] is True


def test_semantic_catalog_supports_arbitrary_clinic_device_keys() -> None:
    context = build_semantic_context(
        {
            "services": [
                {
                    "id": "service-laser",
                    "name": "ليزر جسم كامل",
                    "requires_laser_device": True,
                    "laser_devices": [
                        {
                            "device_key": "device_deka_again",
                            "device_name": "DEKA Again",
                        }
                    ],
                }
            ],
            "doctors": [],
            "appointments": [],
            "packages": [],
        }
    )

    assert context.model_input["devices"] == [{"ref": "V1", "name": "DEKA Again"}]
    assert context.model_input["service_device_refs"] == {"S1": ["V1"]}
    assert context.resolve("V1", expected_kind="device") == "device_deka_again"
