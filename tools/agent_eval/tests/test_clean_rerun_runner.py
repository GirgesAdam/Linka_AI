from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from tools.agent_eval import run_batch_01 as batch
from tools.agent_eval import run_batch_01_clean_rerun as clean


def test_clean_rerun_module_contract() -> None:
    assert callable(clean.main)
    assert clean.AFFECTED == [
        batch.case_ambiguous_laser,
        batch.case_package_other_service,
        batch.case_multi_question,
    ]
    assert callable(clean._token_summary)
    assert callable(clean.run_case)


def test_bookable_context_resolves_canonical_db_id_into_agent_catalog(monkeypatch) -> None:
    service_id = UUID("11111111-1111-1111-1111-111111111111")
    doctor_id = UUID("22222222-2222-2222-2222-222222222222")
    branch_id = UUID("33333333-3333-3333-3333-333333333333")
    workspace = SimpleNamespace(id=UUID("44444444-4444-4444-4444-444444444444"))
    service_row = SimpleNamespace(
        id=service_id,
        slug="prp-skin",
        price_minor=200000,
        is_active=True,
    )
    catalog = {
        "branches": [{"id": str(branch_id), "name": "Linka Clinic"}],
        "services": [{"id": str(service_id), "name": "PRP للبشرة", "requires_laser_device": False}],
        "doctors": [{
            "id": str(doctor_id),
            "name": "د. Test",
            "service_ids": [str(service_id)],
            "scheduled_branch_ids": [str(branch_id)],
        }],
    }
    slot = SimpleNamespace(start_at=SimpleNamespace(isoformat=lambda: "2026-09-30T10:00:00+00:00"))
    available = SimpleNamespace(slots=[slot])

    class Result:
        def __init__(self, value):
            self.value = value
        def scalar_one_or_none(self):
            return self.value
        def mappings(self):
            return self
        def all(self):
            return []

    class DB:
        def execute(self, statement, params):
            if "slug" in params:
                return Result(service_id)
            return Result(None)
        def get(self, model, key):
            return service_row if key == service_id else None

    adapter = SimpleNamespace(
        require_capability=lambda capability: None,
        get_availability=lambda request: available,
    )
    monkeypatch.setattr(clean.harness, "build_clinic_catalog", lambda db, ws: catalog)
    monkeypatch.setattr(clean, "get_clinic_adapter", lambda **kwargs: adapter)
    monkeypatch.setattr(clean.batch, "doctor_name", lambda doctor: doctor["name"])

    _, service, doctor, resolved_branch, _, result = clean._bookable_context(
        DB(), workspace, service_slug="prp-skin"
    )
    assert service["id"] == str(service_id)
    assert doctor["id"] == str(doctor_id)
    assert resolved_branch == str(branch_id)
    assert result is available
    assert clean._fixture_context["fixture_valid"] is True
    assert clean._fixture_context["service_slug"] == "prp-skin"


def test_report_serializer_output_path_is_callable() -> None:
    assert callable(clean.harness.jsonable)
    payload = {"value": UUID("55555555-5555-5555-5555-555555555555")}
    assert clean.harness.jsonable(payload) == {
        "value": "55555555-5555-5555-5555-555555555555"
    }
