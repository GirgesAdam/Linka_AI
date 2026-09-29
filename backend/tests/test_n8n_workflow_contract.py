import json
from pathlib import Path

WORKFLOW_NAMES = (
    "tia_whatsapp_outbox_worker.json",
    "tia_automation_scheduler.json",
)


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_workflows_are_valid_json_without_embedded_credentials() -> None:
    for name in WORKFLOW_NAMES:
        path = _root() / "n8n" / "workflows" / name
        workflow = json.loads(path.read_text(encoding="utf-8"))
        assert workflow["nodes"]
        raw = json.dumps(workflow, ensure_ascii=False).lower()
        for forbidden in (
            "tia_ch_",
            'x-automation-token":',
            'access_token":',
            'client_secret":',
            "bearer eaa",
        ):
            assert forbidden not in raw


def test_whatsapp_transport_worker_only_wakes_tia_native_transport() -> None:
    root = _root() / "n8n" / "workflows"
    worker = json.loads((root / "tia_whatsapp_outbox_worker.json").read_text(encoding="utf-8"))
    raw = json.dumps(worker, ensure_ascii=False)

    assert "/api/v1/channels/whatsapp/transport/tick" in raw
    assert "n8n-nodes-base.whatsApp" not in raw
    assert "graph.facebook.com" not in raw
    assert "/adapter/outbox/claim" not in raw
    assert "/adapter/outbox/provider-status" not in raw


def test_meta_webhook_and_provider_results_are_owned_by_backend() -> None:
    root = _root()
    route = (root / "backend/app/api/routes/whatsapp_setup.py").read_text(encoding="utf-8")
    transport = (root / "backend/app/services/meta_whatsapp_transport.py").read_text(encoding="utf-8")

    assert '@router.get("/webhook/{connection_id}"' in route
    assert '@router.post("/webhook/{connection_id}"' in route
    assert "X-Hub-Signature-256" in route
    assert "app_secret_ciphertext" in route
    assert "_verify_scoped_signature" in route
    assert "record_dispatch_result(" in transport
    assert "record_provider_status(" in transport
    assert '_graph_url(f"{phone_number_id}/messages")' in transport


def test_runtime_workers_support_a_hosted_backend_url() -> None:
    root = _root() / "n8n" / "workflows"
    for name in WORKFLOW_NAMES:
        raw = (root / name).read_text(encoding="utf-8")
        assert "$env.TIA_API_BASE_URL" in raw
        assert "|| 'http://host.docker.internal:8000'" in raw
        assert "https://YOUR_TIA_BACKEND_DOMAIN" not in raw


def test_automation_scheduler_execute_url_is_a_real_expression() -> None:
    path = _root() / "n8n" / "workflows" / "tia_automation_scheduler.json"
    workflow = json.loads(path.read_text(encoding="utf-8"))
    by_name = {node["name"]: node for node in workflow["nodes"]}

    execute_url = by_name["Linka Execute Automation"]["parameters"]["url"]
    assert "$env.TIA_API_BASE_URL" in execute_url
    assert "/api/v1/automations/adapter/jobs/" in execute_url
    assert "+ $json.job_id +" in execute_url
    assert "%27%20%2B%20%24json.job_id" not in execute_url


def test_inbound_whatsapp_is_not_processed_or_retried_by_n8n() -> None:
    root = _root()
    workflows = root / "n8n" / "workflows"
    transport = (root / "backend/app/services/meta_whatsapp_transport.py").read_text(encoding="utf-8")
    channels = (root / "backend/app/services/channels.py").read_text(encoding="utf-8")

    assert not (workflows / "tia_whatsapp_inbound_status.json").exists()
    assert "def ingest_meta_webhook(" in transport
    assert "def _process_pending_inbound(" in transport
    assert "MAX_INBOUND_PROCESS_ATTEMPTS," in transport
    assert "MAX_INBOUND_PROCESS_ATTEMPTS = 3" in channels


def test_provider_send_retries_remain_in_tia_state_machine_not_n8n() -> None:
    root = _root()
    worker = (root / "n8n/workflows/tia_whatsapp_outbox_worker.json").read_text(encoding="utf-8")
    transport = (root / "backend/app/services/meta_whatsapp_transport.py").read_text(encoding="utf-8")

    assert "WhatsApp Send Template" not in worker
    assert "WhatsApp Send Text" not in worker
    assert "retry_after_seconds=30" in transport
    assert "record_dispatch_result(" in transport


def test_real_runtime_docs_match_native_whatsapp_architecture() -> None:
    root = _root()
    for relative in (
        "n8n/REAL_RUNTIME_SETUP.md",
        "n8n/WHATSAPP_SETUP.md",
        "deploy/oracle-n8n/README.md",
    ):
        docs = (root / relative).read_text(encoding="utf-8")
        assert "tia_automation_scheduler.json" in docs
        assert "tia-whatsapp-transport-waker" in docs
        assert "/api/v1/channels/whatsapp/transport/tick" in docs
        assert "tia_whatsapp_inbound_status.json" not in docs
        assert "/adapter/outbox/claim" not in docs
        assert "n8n WhatsApp Trigger" not in docs
        assert "n8n WhatsApp Business Cloud API credential" not in docs


def test_oracle_setup_can_only_import_and_wire_scheduler() -> None:
    root = _root() / "deploy" / "oracle-n8n"
    importer = (root / "import-production-workflows.sh").read_text(encoding="utf-8")
    wiring = (root / "wire-tia-runtime.sh").read_text(encoding="utf-8")
    token_generator = (root / "generate-runtime-tokens.sh").read_text(encoding="utf-8")

    for raw in (importer, wiring):
        assert "tia_automation_scheduler.json" in raw
        assert "tia_whatsapp_inbound_status.json" not in raw
        assert "tia_whatsapp_outbox_worker.json" not in raw
        assert "TIA_CHANNEL_TOKEN" not in raw
        assert "X-Channel-Token" not in raw

    assert "TIA_AUTOMATION_TOKEN" in token_generator
    assert "TIA_CHANNEL_TOKEN" not in token_generator
    assert "tia_ch_" not in token_generator

    for retired in (
        "configure-meta-whatsapp.sh",
        "publish-whatsapp-inbound.sh",
        "publish-whatsapp-outbox.sh",
        "repair-whatsapp-outbox-results.sh",
    ):
        assert not (root / retired).exists()


def test_oracle_publish_guardrail_unpublishes_any_legacy_adapter_workflow() -> None:
    script = (_root() / "deploy/oracle-n8n/publish-scheduler.sh").read_text(encoding="utf-8")

    assert "publish:workflow --id=tiaAutoSched0001" in script
    assert "unpublish:workflow --id=tiaWAInbound0001" in script
    assert "unpublish:workflow --id=tiaWAOutbox00001" in script
    assert "CAST(nodes AS text) LIKE '%/adapter/outbox/claim%'" in script
    assert "CAST(nodes AS text) LIKE '%/adapter/outbox/provider-status%'" in script
    assert "CAST(nodes AS text) LIKE '%/channels/adapter/inbound%'" in script
    assert 'n8n unpublish:workflow --id="$workflow_id"' in script


def test_oracle_healthcheck_enforces_and_repairs_scheduler_only_state() -> None:
    script = (_root() / "deploy/oracle-n8n/healthcheck-production.sh").read_text(encoding="utf-8")

    assert "id = 'tiaAutoSched0001' AND active IS TRUE" in script
    assert "LEGACY_ACTIVE_COUNT" in script
    assert "bash ./publish-scheduler.sh" in script
    assert "id IN ('tiaWAInbound0001','tiaWAOutbox00001')" in script
    assert "Expected 3 active Linka production workflows" not in script


def test_oracle_backup_verifier_requires_scheduler_and_no_active_legacy_transport() -> None:
    script = (_root() / "deploy/oracle-n8n/verify-production-backup.sh").read_text(encoding="utf-8")

    assert "^TIA_AUTOMATION_TOKEN=.+" in script
    assert "SCHEDULER_ACTIVE_COUNT" in script
    assert "LEGACY_ACTIVE_COUNT" in script
    assert "id = 'tiaAutoSched0001' AND active IS TRUE" in script
    assert "Expected 3 active Linka production workflows" not in script
