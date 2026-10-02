from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def test_laser_service_creation_uses_clinic_device_registry() -> None:
    form = (FRONTEND / "src/app/(dashboard)/services/service-create-form.tsx").read_text(
        encoding="utf-8"
    )
    actions = (FRONTEND / "src/app/(dashboard)/services/actions.ts").read_text(
        encoding="utf-8"
    )
    page = (FRONTEND / "src/app/(dashboard)/services/page.tsx").read_text(
        encoding="utf-8"
    )

    assert "useState(false)" in form
    assert "requiresLaserDevice ? (" in form
    assert "ClinicLaserDevice" in form
    assert "activeDevices.map" in form
    assert 'name="device_key"' in form
    assert "device_enabled_" in form
    assert "device_price_" in form
    assert "device_duration_" in form
    assert 'name="price"' in form
    assert 'name="duration_minutes"' in form

    assert "ServiceCreateForm" in page
    assert 'tiaRequest<ClinicLaserDevice[]>("/inventory/laser-devices")' in page
    assert "devices={devices}" in page

    assert "selectedDeviceKeys" in actions
    assert "devicePricePayload" in actions
    assert "selectedDevices.map" in actions
    assert 'tiaRequest("/inventory/laser-prices"' in actions
    assert "device_key: deviceKey" in actions or "...devicePricePayload" in actions

    # Prime/Candela are migration/backward-compatibility data, not hardcoded service-form fields.
    assert 'name="prime_lase_price"' not in form
    assert 'name="candela_gentle_price"' not in form
    assert 'device_key: "prime_lase"' not in actions
    assert 'device_key: "candela_gentle"' not in actions
