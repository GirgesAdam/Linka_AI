from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = ROOT / ".github" / "benchmark"
BACKEND_DIR = ROOT / "backend"

EXECUTION_STATUSES = {"NOT_RUN", "COMPLETED", "RUNTIME_ERROR", "ENVIRONMENT_ERROR", "LIVE_REFUSED"}

# Existing legacy probes whose customer path has been reviewed as read-only and can be
# reused for raw evidence. This is deliberately a small safe subset; absence here is not
# a semantic failure and must never be interpreted as one.
SAFE_READ_ONLY_PROBES: dict[str, str] = {
    "RM04": "nearest_available",
    "RM06": "conditional_fallback",
    "RM11": "change_service_mid_conversation",
    "RM20": "no_show_semantics",
    "RM29": "out_of_scope_recovery",
}

SECRET_KEY_NAMES = {
    "openai_api_key", "api_key", "authorization", "access_token", "refresh_token",
    "password", "database_url", "migration_database_url", "connection_string",
    "webhook_secret", "supabase_secret_key", "secret_ref",
}
_SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bsb_secret_[A-Za-z0-9_-]+\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
)


class LiveSafetyError(RuntimeError):
    pass


def _contract_for_execution(document: dict[str, Any], execution_id: str) -> tuple[str, dict[str, Any]]:
    parents = {item["scenario_id"]: item for item in document["contracts"]}
    if execution_id in parents:
        return execution_id, parents[execution_id]
    for parent_id, parent in parents.items():
        for subcase in parent.get("subcases", []):
            if subcase.get("subcase_id") == execution_id:
                return parent_id, subcase
    raise KeyError(execution_id)


def execution_readiness(document: dict[str, Any], execution_id: str) -> dict[str, Any]:
    coverage_id, contract = _contract_for_execution(document, execution_id)
    mode = contract["expected_read_write_policy"]["mode"]
    source = SAFE_READ_ONLY_PROBES.get(execution_id)
    if mode == "write_after_explicit_authorization":
        return {
            "execution_id": execution_id,
            "coverage_id": coverage_id,
            "policy": mode,
            "safe_to_execute": False,
            "source_scenario": None,
            "reason": "Write contracts remain blocked for current live QA; rollback proof is not live-write authorization.",
        }
    if source is None:
        return {
            "execution_id": execution_id,
            "coverage_id": coverage_id,
            "policy": mode,
            "safe_to_execute": False,
            "source_scenario": None,
            "reason": "No independently reviewed canonical/legacy execution adapter exists for this contract yet.",
        }
    return {
        "execution_id": execution_id,
        "coverage_id": coverage_id,
        "policy": mode,
        "safe_to_execute": True,
        "source_scenario": source,
        "reason": "Reviewed read-only legacy probe; raw evidence only, never semantic PASS/FAIL.",
    }


def _sanitize_string(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_artifact(value: Any, *, key: str | None = None) -> Any:
    normalized_key = (key or "").lower()
    if normalized_key in SECRET_KEY_NAMES or normalized_key.endswith(("_password", "_secret")):
        return "[REDACTED]"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _sanitize_string(value)
    if is_dataclass(value):
        return redact_artifact(asdict(value), key=key)
    if hasattr(value, "model_dump"):
        return redact_artifact(value.model_dump(mode="json"), key=key)
    if isinstance(value, dict):
        return {str(k): redact_artifact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_artifact(v) for v in value]
    return _sanitize_string(str(value))


def token_usage_from_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [
        call
        for turn in turns
        for call in list((turn.get("token_usage") or {}).get("calls") or [])
        if isinstance(call, dict)
    ]
    return {
        "input_tokens": sum(int((turn.get("token_usage") or {}).get("input_tokens") or 0) for turn in turns),
        "output_tokens": sum(int((turn.get("token_usage") or {}).get("output_tokens") or 0) for turn in turns),
        "total_tokens": sum(int((turn.get("token_usage") or {}).get("total_tokens") or 0) for turn in turns),
        "call_count": len(calls),
        "calls": calls,
    }


def raw_execution_artifact(
    *,
    execution_id: str,
    coverage_id: str,
    git_sha: str | None,
    raw: dict[str, Any],
    execution_status: str,
    source_scenario: str | None,
    environment_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if execution_status not in EXECUTION_STATUSES:
        raise ValueError(f"Unsupported execution status: {execution_status}")
    turns = list(raw.get("turns") or [])
    customer_messages = list(raw.get("customer_messages") or [turn.get("customer") for turn in turns if turn.get("customer") is not None])
    artifact = {
        "scenario_id": execution_id,
        "coverage_id": coverage_id,
        "source_scenario": source_scenario,
        "git_sha": git_sha,
        "execution_status": execution_status,
        "customer_messages": customer_messages,
        "turns": turns,
        "db_before": raw.get("db_before") or {},
        "db_after": raw.get("db_after") or {},
        "db_delta": raw.get("db_delta") or {},
        "observations": list(raw.get("observations") or []),
        "runtime_error": raw.get("runtime_error"),
        "token_usage": raw.get("token_usage") or token_usage_from_turns(turns),
        "environment_evidence": environment_evidence or {},
    }
    return redact_artifact(artifact)


def execute_with_backend(
    *,
    document: dict[str, Any],
    execution_id: str,
    git_sha: str | None,
    backend: Callable[[str, str], dict[str, Any]],
    environment_evidence: dict[str, Any] | None = None,
    enforce_live_readiness: bool = True,
) -> dict[str, Any]:
    readiness = execution_readiness(document, execution_id)
    if enforce_live_readiness and not readiness["safe_to_execute"]:
        return raw_execution_artifact(
            execution_id=execution_id,
            coverage_id=readiness["coverage_id"],
            git_sha=git_sha,
            raw={"runtime_error": readiness["reason"]},
            execution_status="LIVE_REFUSED",
            source_scenario=readiness["source_scenario"],
            environment_evidence=environment_evidence,
        )
    source = readiness["source_scenario"] or "stub-only"
    try:
        raw = backend(execution_id, source)
    except Exception as exc:  # noqa: BLE001 - operational boundary only, not semantic judgment
        raw = {"runtime_error": f"{type(exc).__name__}: {exc}"}
        status = "RUNTIME_ERROR"
    else:
        status = "RUNTIME_ERROR" if raw.get("runtime_error") else "COMPLETED"
    return raw_execution_artifact(
        execution_id=execution_id,
        coverage_id=readiness["coverage_id"],
        git_sha=git_sha,
        raw=raw,
        execution_status=status,
        source_scenario=source,
        environment_evidence=environment_evidence,
    )


def verify_external_effect_isolation() -> dict[str, Any]:
    core_source = (BENCHMARK_DIR / "v2_benchmark_core.py").read_text(encoding="utf-8")
    live_source = (BACKEND_DIR / "app" / "services" / "agent_v2" / "live_chat.py").read_text(encoding="utf-8")
    required = {
        "benchmark_uses_direct_agent_api": "live_chat.run_agent_chat(" in core_source,
        "direct_api_outbound_not_queued": 'outbound_delivery_status="sent"' in live_source,
        "direct_api_source_is_agent_api": 'source="agent_api"' in live_source,
        "dispatch_flag_depends_on_queue": '"dispatch_required": outbound_delivery_status == "queued"' in live_source,
    }
    if not all(required.values()):
        missing = sorted(key for key, ok in required.items() if not ok)
        raise LiveSafetyError("External-effect isolation proof changed: " + ", ".join(missing))
    return {
        **required,
        "transport_path": "run_agent_chat/agent_api",
        "provider_dispatch": "not queued by the selected benchmark path",
        "write_execution_currently_allowed": False,
    }


def _normalize_sqlalchemy_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url[len("postgres://"):]
    return database_url


def verify_runtime_database_identity(
    database_url: str,
    *,
    expected_database_name: str,
    expected_marker: str,
    expected_workspace_id: str,
    expected_workspace_slug: str,
) -> tuple[Any, dict[str, Any]]:
    """Open the dedicated QA DB and prove runtime identity before any model call."""
    try:
        from sqlalchemy import create_engine, text
    except Exception as exc:  # pragma: no cover - exercised in installed CI/live env
        raise LiveSafetyError(f"SQLAlchemy unavailable for runtime DB identity check: {type(exc).__name__}") from exc

    engine = create_engine(_normalize_sqlalchemy_url(database_url), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            identity = connection.execute(text(
                "select current_database() as database_name, "
                "coalesce(inet_server_addr()::text, 'local') as server_address, "
                "inet_server_port() as server_port"
            )).mappings().one()
            if str(identity["database_name"]) != expected_database_name:
                raise LiveSafetyError("Runtime database name does not match the declared QA database.")
            marker_table = connection.scalar(text("select to_regclass('public.linka_qa_environment_marker')"))
            if marker_table is None:
                raise LiveSafetyError("Dedicated QA runtime marker table is absent.")
            marker = connection.scalar(text(
                "select marker_value from public.linka_qa_environment_marker "
                "where marker_key='selected_agent_runner'"
            ))
            if marker != expected_marker:
                raise LiveSafetyError("Dedicated QA runtime marker does not match the approved marker.")
            workspace = connection.execute(text(
                "select id::text as id, slug, is_demo from workspaces "
                "where id::text=:workspace_id and slug=:slug"
            ), {"workspace_id": expected_workspace_id, "slug": expected_workspace_slug}).mappings().first()
            if workspace is None:
                raise LiveSafetyError("Expected dedicated QA workspace identity was not found.")
            if workspace["is_demo"] is not True:
                raise LiveSafetyError("Dedicated QA workspace must be marked is_demo=true.")
            integration = connection.execute(text(
                "select mode, adapter_key, status from clinic_integrations where workspace_id=:workspace_id"
            ), {"workspace_id": expected_workspace_id}).mappings().first()
            if integration is None:
                raise LiveSafetyError("QA workspace must have an explicit clinic integration row.")
            if not (
                integration["mode"] == "tia_native"
                and integration["adapter_key"] == "tia_database"
                and integration["status"] == "active"
            ):
                raise LiveSafetyError("QA workspace must use active tia_native/tia_database integration.")
            evidence = {
                "database_name": str(identity["database_name"]),
                "server_address": str(identity["server_address"]),
                "server_port": int(identity["server_port"]),
                "qa_marker_verified": True,
                "workspace_id": str(workspace["id"]),
                "workspace_slug": str(workspace["slug"]),
                "workspace_is_demo": bool(workspace["is_demo"]),
                "clinic_integration": {
                    "mode": integration["mode"],
                    "adapter_key": integration["adapter_key"],
                    "status": integration["status"],
                },
            }
        return engine, evidence
    except Exception:
        engine.dispose()
        raise


def verify_outer_transaction_rollback(engine: Any) -> dict[str, Any]:
    """Prove nested Session.commit() stays inside an outer rollback on this exact DB."""
    try:
        from sqlalchemy import text
        from sqlalchemy.orm import Session
    except Exception as exc:  # pragma: no cover
        raise LiveSafetyError(f"SQLAlchemy unavailable for rollback proof: {type(exc).__name__}") from exc
    import uuid

    probe_id = "selected-agent-" + uuid.uuid4().hex
    with engine.connect() as check:
        table = check.scalar(text("select to_regclass('public.linka_qa_rollback_probe')"))
        if table is None:
            raise LiveSafetyError("Dedicated QA rollback probe table is absent.")
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    visible_during = False
    try:
        session.execute(
            text("insert into public.linka_qa_rollback_probe(probe_id) values (:probe_id)"),
            {"probe_id": probe_id},
        )
        session.commit()
        visible_during = bool(connection.scalar(text(
            "select exists(select 1 from public.linka_qa_rollback_probe where probe_id=:probe_id)"
        ), {"probe_id": probe_id}))
        if not visible_during:
            raise LiveSafetyError("Rollback probe write was not visible inside the scenario transaction.")
    finally:
        session.close()
        if outer.is_active:
            outer.rollback()
        connection.close()
    with engine.connect() as verification:
        persisted_after = bool(verification.scalar(text(
            "select exists(select 1 from public.linka_qa_rollback_probe where probe_id=:probe_id)"
        ), {"probe_id": probe_id}))
    if persisted_after:
        raise LiveSafetyError("Outer rollback failed: QA rollback probe persisted.")
    return {
        "write_visible_during_transaction": visible_during,
        "persisted_after_outer_rollback": persisted_after,
        "join_transaction_mode": "create_savepoint",
        "rollback_verified": True,
    }


def _assemble_legacy_scenario_module() -> Path:
    parts = sorted(BENCHMARK_DIR.glob("v2_benchmark_scenarios.part.*"))
    if not parts:
        raise RuntimeError("Benchmark scenario parts are missing.")
    target = Path(tempfile.gettempdir()) / "v2_benchmark_scenarios.py"
    target.write_bytes(b"".join(part.read_bytes() for part in parts))
    return target


def _load_module_from_path(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def execute_real_agent_probe(engine: Any, execution_id: str, source_scenario: str) -> dict[str, Any]:
    """Run one reviewed legacy probe through real Agent V2 capture; caller owns safety gates."""
    for path in (str(BACKEND_DIR), str(BENCHMARK_DIR), str(ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    scenario_module = _assemble_legacy_scenario_module()
    _load_module_from_path("v2_benchmark_scenarios", scenario_module)
    core = _load_module_from_path("v2_benchmark_core_selected", BENCHMARK_DIR / "v2_benchmark_core.py")
    result = core.execute_one(engine, source_scenario, execution_id)
    turns = [core.jsonable(turn) for turn in result.turns]
    return {
        "customer_messages": [turn.get("customer") for turn in turns],
        "turns": turns,
        "db_before": core.jsonable(result.db_before),
        "db_after": core.jsonable(result.db_after),
        "db_delta": core.jsonable(result.db_delta),
        "observations": core.jsonable(result.observations),
        "runtime_error": result.error,
        "token_usage": token_usage_from_turns(turns),
    }
