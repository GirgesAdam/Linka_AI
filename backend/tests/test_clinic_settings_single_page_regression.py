from types import SimpleNamespace
from uuid import uuid4

from app.schemas.clinic_knowledge_base import ClinicKnowledgeText
from app.services import clinic_knowledge_base


def test_single_knowledge_contract_allows_empty_and_caps_content() -> None:
    assert ClinicKnowledgeText().content == ""
    assert ClinicKnowledgeText(content="معلومات العيادة").content == "معلومات العيادة"
    assert ClinicKnowledgeText.model_fields["content"].metadata


def test_read_single_knowledge_text_preserves_legacy_entries(monkeypatch) -> None:
    entries = [
        SimpleNamespace(title="عن العيادة", content="معلومة عامة"),
        SimpleNamespace(title="جلسة ليزر", content="شرح الجلسة"),
    ]
    monkeypatch.setattr(
        clinic_knowledge_base,
        "list_knowledge_entries",
        lambda _db, *, workspace_id: entries,
    )

    content = clinic_knowledge_base.read_knowledge_text(object(), workspace_id=uuid4())

    assert "عن العيادة\nمعلومة عامة" in content
    assert "جلسة ليزر\nشرح الجلسة" in content


def test_replace_single_knowledge_text_creates_one_clinic_entry() -> None:
    class FakeDB:
        def __init__(self):
            self.added = []
            self.flush_calls = 0
            self.executed = []

        def execute(self, statement):
            self.executed.append(statement)

        def flush(self):
            self.flush_calls += 1

        def add(self, value):
            self.added.append(value)

    db = FakeDB()
    workspace_id = uuid4()

    result = clinic_knowledge_base.replace_knowledge_text(
        db,
        workspace_id=workspace_id,
        content="  كل المعلومات في خانة واحدة  ",
    )

    assert result == "كل المعلومات في خانة واحدة"
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.workspace_id == workspace_id
    assert entry.scope_type == "clinic"
    assert entry.service_id is None
    assert entry.device_key is None
    assert entry.title == clinic_knowledge_base.SINGLE_KNOWLEDGE_TITLE
    assert entry.content == result
    assert db.flush_calls == 2


def test_clinic_settings_ui_has_no_booking_policy_or_shortcuts() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    page = (root / "frontend/src/app/(dashboard)/setup/page.tsx").read_text(encoding="utf-8")
    panel = (root / "frontend/src/app/(dashboard)/setup/clinic-settings-panel.tsx").read_text(encoding="utf-8")
    actions = (root / "frontend/src/app/(dashboard)/setup/clinic-settings-actions.ts").read_text(encoding="utf-8")
    knowledge_page = (root / "frontend/src/app/(dashboard)/knowledge/page.tsx").read_text(encoding="utf-8")
    history_page = (root / "frontend/src/app/(dashboard)/setup/integration/page.tsx").read_text(encoding="utf-8")

    assert "سياسة الحجز" not in page + panel
    assert "saveBookingPolicyFormAction" not in actions
    assert 'href="/services"' not in panel
    assert 'href="/doctors"' not in panel
    assert 'href="/setup/integration"' not in panel
    assert 'id="tia-knowledge"' in panel
    assert panel.count('name="content"') == 1
    assert 'redirect("/setup#tia-knowledge")' in knowledge_page
    assert 'redirect("/setup#historical-data")' in history_page
    assert "HistoricalImportUploader" in panel


def test_large_clinic_settings_sections_are_collapsible_by_header() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    panel = (root / "frontend/src/app/(dashboard)/setup/clinic-settings-panel.tsx").read_text(encoding="utf-8")

    for key in ("clinic-hours", "linka-knowledge", "historical-data"):
        marker = f'data-collapsible-section="{key}"'
        assert marker in panel
        details_tag = panel[panel.index(marker) - 20 : panel.index(marker) + len(marker) + 80]
        assert " open" not in details_tag
    assert panel.count("<summary className=") >= 3
    assert "group-open:rotate-180" in panel
