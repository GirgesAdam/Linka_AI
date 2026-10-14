from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"


def test_laser_service_creation_uses_category_and_single_atomic_service_request() -> None:
    create_form = (
        FRONTEND / "src/app/(dashboard)/services/service-create-form.tsx"
    ).read_text(encoding="utf-8")
    edit_form = (
        FRONTEND / "src/app/(dashboard)/services/service-pricing-form.tsx"
    ).read_text(encoding="utf-8")
    actions = (FRONTEND / "src/app/(dashboard)/services/actions.ts").read_text(
        encoding="utf-8"
    )
    route = (BACKEND / "app/api/routes/clinic.py").read_text(encoding="utf-8")
    page = (FRONTEND / "src/app/(dashboard)/services/page.tsx").read_text(
        encoding="utf-8"
    )

    assert 'name="requires_laser_device"' not in create_form
    assert 'name="requires_laser_device"' not in edit_form
    assert 'const requiresLaserDevice = category === "laser"' in create_form
    assert 'const requiresLaserDevice = category === "laser"' in edit_form
    assert "useActionState(" in create_form
    assert "createService(previous, formData)" in create_form
    assert "useActionState(" in edit_form
    assert "updateServicePricing(previous, formData)" in edit_form
    assert "router.refresh()" in create_form
    assert "router.refresh()" in edit_form
    assert "disabled={pending}" in create_form
    assert "disabled={pending}" in edit_form

    assert "ClinicLaserDevice" in create_form
    assert "activeDevices.map" in create_form
    assert 'name="device_key"' in create_form
    assert "device_enabled_" in create_form
    assert "device_price_" in create_form
    assert "device_duration_" in create_form

    assert "ServiceCreateForm" in page
    assert 'tiaRequest<ClinicLaserDevice[]>("/inventory/laser-devices")' in page
    assert "devices={devices}" in page

    create_block = actions[actions.index("export async function createService"):actions.index("export async function updateServicePricing")]
    update_block = actions[actions.index("export async function updateServicePricing"):actions.index("export async function updateLaserDevicePrice")]
    assert 'tiaRequest("/clinic/services"' in create_block
    assert 'tiaRequest("/inventory/laser-prices"' not in create_block
    assert 'tiaRequest("/inventory/laser-prices"' not in update_block
    assert "device_prices:" in create_block
    assert "device_prices:" in update_block
    assert "selectedDevices.map" in create_block
    assert "selectedDevices.map" in update_block

    create_route = route[route.index('@router.post("/services"'):route.index('@router.get("/services"')]
    assert "_sync_service_device_prices(" in create_route
    assert create_route.index("_sync_service_device_prices(") < create_route.index("commit_or_conflict(")
    assert 'service_values["requires_laser_device"] = payload.operational_category == "laser"' in create_route

    # Prime/Candela are migration/backward-compatibility data, not hardcoded service-form fields.
    assert 'name="prime_lase_price"' not in create_form
    assert 'name="candela_gentle_price"' not in create_form
    assert 'device_key: "prime_lase"' not in actions
    assert 'device_key: "candela_gentle"' not in actions


def test_history_import_ui_uses_structured_validation_errors_and_concise_summary() -> None:
    api = (FRONTEND / "src/lib/tia/api.ts").read_text(encoding="utf-8")
    actions = (
        FRONTEND / "src/app/(dashboard)/setup/integration/actions.ts"
    ).read_text(encoding="utf-8")
    uploader = (
        FRONTEND / "src/app/(dashboard)/setup/integration/history-uploader.tsx"
    ).read_text(encoding="utf-8")

    assert 'code: "import_validation_failed"' in api
    assert "getImportValidationErrorDetail" in api
    assert "public detail?: unknown" in api
    assert "getImportValidationErrorDetail(error)" in actions
    assert "MAX_VISIBLE_VALIDATION_ERRORS = 4" in actions
    assert 'item.row ? `- الصف ${item.row}:' in actions
    assert "مشاكل إضافية" in actions
    assert "whitespace-pre-line" in uploader
    assert "MAX_VISIBLE_ISSUE_GROUPS = 4" in uploader
    assert "issue_groups.slice" in uploader
    assert "وهناك {hiddenIssueRows} مشاكل إضافية" in uploader


def test_laser_category_invariant_is_backfilled_and_database_enforced() -> None:
    service_model = (BACKEND / "app/models/service.py").read_text(encoding="utf-8")
    migration = (
        BACKEND / "alembic/versions/0095_service_laser_category.py"
    ).read_text(encoding="utf-8")

    assert "service_laser_requirement_matches_category" in service_model
    assert "requires_laser_device = (operational_category = 'laser')" in service_model
    assert "SET requires_laser_device = (operational_category = 'laser')" in migration
    assert "UPDATE service_device_prices AS prices" in migration
    assert "SET is_active = false" in migration
    assert "services.operational_category <> 'laser'" in migration
