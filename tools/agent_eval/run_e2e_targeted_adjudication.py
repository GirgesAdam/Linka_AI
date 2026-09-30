from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.clinic_grounding import build_clinic_catalog
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.patient import Patient
from app.models.workspace import Workspace
from tools.agent_eval.harness import (
    acquire_eval_advisory_lock,
    assert_demo_only,
    local_slot,
    service_by_slug,
)
from tools.agent_eval.run_batch_01 import created_appointments, doctor_name
from tools.agent_eval.run_batch_03 import (
    _appointment_by_id,
    _future_slot,
    _run_messages,
    _seed_future_appointment,
    extended_state_snapshot,
)
from tools.agent_eval.run_batch_04 import _availability_for, _doctor_row, _replacement_rows


def patient_id(db: Session, value: str) -> Patient:
    row = db.get(Patient, UUID(value))
    if row is None:
        raise RuntimeError("patient missing")
    return row


def compact(sid, turns, before, after, extra=None):
    return {
        "id": sid,
        "turns": [{
            "user": t.user_message,
            "reply": t.agent_response,
            "model": t.model,
            "reads": t.verified_reads,
            "write_attempted": t.write_attempted,
            "write_result": t.write_result,
            "handoff": t.handoff_state,
            "latency_ms": t.latency_ms,
            "tokens": t.token_usage,
            "llm_calls": t.llm_calls,
        } for t in turns],
        "before": before,
        "after": after,
        "extra": extra or {},
    }


def run_isolated(engine, fn):
    conn = engine.connect()
    outer = conn.begin()
    db = Session(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        acquire_eval_advisory_lock(db, namespace="linka-e2e-targeted-20260930")
        ws = db.scalar(select(Workspace).where(Workspace.slug == "tia"))
        assert_demo_only(ws)
        return fn(db, ws)
    except Exception as exc:  # noqa: BLE001
        return {"id": fn.__name__, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()
        if outer.is_active:
            outer.rollback()
        conn.close()


def t01_reem_actual(db, ws):
    p = patient_id(db, "dfc472f0-6ca0-5356-8ed2-0ea986c5bb3f")
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t01_reem_actual",
        ["عندي ايه جاي الأسبوع ده؟ قولي المواعيد والدكاترة"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t01_reem_actual", turns, before, after)


def t02_omar_package_actual(db, ws):
    p = patient_id(db, "3a55514e-1f58-58aa-bd91-13f0fc230062")
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t02_omar_package_actual",
        ["باكدج الـPRP للبشرة بتاعتي فاضل فيها كام جلسة وصالحة لحد امتى؟"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t02_omar_package_actual", turns, before, after)


def t03_laila_exhausted_actual(db, ws):
    p = patient_id(db, "65035dbe-fda9-511c-8200-0a10822af645")
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t03_laila_exhausted_actual",
        ["باكدج الهيدرافيشل بتاعتي خلصت ولا لسه فيها جلسات؟"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t03_laila_exhausted_actual", turns, before, after)


def t04_package_booking_actual(db, ws):
    p = patient_id(db, "3a55514e-1f58-58aa-bd91-13f0fc230062")
    service = service_by_slug(db, ws, "prp-skin")
    av, slot = _availability_for(db, ws, service=service, after_date=datetime.now(UTC).date() + timedelta(days=2))
    catalog = build_clinic_catalog(db, ws)
    doctor = _doctor_row(catalog, slot.doctor_id)
    day, tm = local_slot(av, slot)
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t04_package_booking_actual",
        [f"احجزيلي جلسة PRP للبشرة من الباكدج بتاعتي يوم {day} الساعة {tm} مع {doctor_name(doctor)}"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t04_package_booking_actual", turns, before, after,
        {"created": created_appointments(before, after)})


def t05_cancel_far(db, ws):
    p = Patient(workspace_id=ws.id, first_name="عميل", last_name="إلغاء بعيد",
        phone="+209700000001", phone_normalized="+209700000001",
        preferred_language="ar", source="other", status="active")
    db.add(p); db.flush()
    service = service_by_slug(db, ws, "hydrafacial")
    av, slot = _availability_for(db, ws, service=service, after_date=datetime.now(UTC).date() + timedelta(days=4))
    appt = _seed_future_appointment(db, ws, p, service=service, doctor_id=UUID(str(slot.doctor_id)), slot=slot)
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t05_cancel_far", ["الغِ ميعادي الجاي خلاص"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t05_cancel_far", turns, before, after,
        {"appointment_after": _appointment_by_id(after, appt.id)})


def t06_cancel_ambiguous_confirm(db, ws):
    p = Patient(workspace_id=ws.id, first_name="عميل", last_name="إلغاء موعدين",
        phone="+209700000002", phone_normalized="+209700000002",
        preferred_language="ar", source="other", status="active")
    db.add(p); db.flush()
    hydra = service_by_slug(db, ws, "hydrafacial")
    prp = service_by_slug(db, ws, "prp-skin")
    av1, s1 = _availability_for(db, ws, service=hydra, after_date=datetime.now(UTC).date() + timedelta(days=4))
    av2, s2 = _availability_for(db, ws, service=prp, after_date=s1.start_at.date())
    a1 = _seed_future_appointment(db, ws, p, service=hydra, doctor_id=UUID(str(s1.doctor_id)), slot=s1)
    a2 = _seed_future_appointment(db, ws, p, service=prp, doctor_id=UUID(str(s2.doctor_id)), slot=s2)
    d2, _ = local_slot(av2, s2)
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t06_cancel_ambiguous_confirm",
        ["عايز الغي واحد من مواعيدي", f"الـPRP اللي يوم {d2}", "ايوه الغيه"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t06_cancel_ambiguous_confirm", turns, before, after,
        {"first": _appointment_by_id(after, a1.id), "second": _appointment_by_id(after, a2.id)})


def t07_conflict_explicit(db, ws):
    p = Patient(workspace_id=ws.id, first_name="عميل", last_name="تعارض صريح",
        phone="+209700000003", phone_normalized="+209700000003",
        preferred_language="ar", source="other", status="active")
    other = Patient(workspace_id=ws.id, first_name="منافس", last_name="تعارض",
        phone="+209700000004", phone_normalized="+209700000004",
        preferred_language="ar", source="other", status="active")
    db.add_all([p, other]); db.flush()
    service = service_by_slug(db, ws, "hydrafacial")
    _av, src_slot = _availability_for(db, ws, service=service, after_date=datetime.now(UTC).date() + timedelta(days=3))
    source = _seed_future_appointment(db, ws, p, service=service, doctor_id=UUID(str(src_slot.doctor_id)), slot=src_slot)
    tav, target = _future_slot(db, ws, service_id=str(service.id), doctor_id=str(source.doctor_id),
        after_date=src_slot.start_at.astimezone(ZoneInfo(ws.timezone or "UTC")).date(),
        exclude_appointment_id=str(source.id))
    _seed_future_appointment(db, ws, other, service=service, doctor_id=UUID(str(source.doctor_id)), slot=target)
    day, tm = local_slot(tav, target)
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t07_conflict_explicit",
        [f"غيري ميعاد الهيدرافيشل بتاعي وخليه يوم {day} الساعة {tm}"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t07_conflict_explicit", turns, before, after,
        {"source": _appointment_by_id(after, source.id), "replacements": _replacement_rows(after, source.id)})


def t08_mixed_recheck(db, ws):
    p = Patient(workspace_id=ws.id, first_name="عميل", last_name="مختلط",
        phone="+209700000005", phone_normalized="+209700000005",
        preferred_language="ar", source="other", status="active")
    db.add(p); db.flush()
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t08_mixed_recheck",
        ["يوم الجمعة شغالين من كام لكام؟ والهيدرافيشل سعرها ومدتها كام؟"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t08_mixed_recheck", turns, before, after)


def t09_package_price_exact(db, ws):
    p = patient_id(db, "3a55514e-1f58-58aa-bd91-13f0fc230062")
    before = extended_state_snapshot(db, ws, p)
    turns, _ = _run_messages(db, ws, p, "t09_package_price_exact",
        ["فاضل كام جلسة PRP للبشرة في الباكدج وسعر الجلسة لوحدها كام؟"])
    after = extended_state_snapshot(db, ws, p)
    return compact("t09_package_price_exact", turns, before, after)


CASES = [t01_reem_actual, t02_omar_package_actual, t03_laila_exhausted_actual,
    t04_package_booking_actual, t05_cancel_far, t06_cancel_ambiguous_confirm,
    t07_conflict_explicit, t08_mixed_recheck, t09_package_price_exact]


def main():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    rows = []
    for fn in CASES:
        print("TARGET_START", fn.__name__, flush=True)
        row = run_isolated(engine, fn)
        rows.append(row)
        print("TARGET_ROW=" + json.dumps(row, ensure_ascii=False, default=str), flush=True)
    out = Path("eval_results") / f"e2e_targeted_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("TARGET_JSON=" + str(out), flush=True)
    engine.dispose()


if __name__ == "__main__":
    main()
