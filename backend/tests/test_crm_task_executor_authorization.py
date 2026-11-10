from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies.security import WorkspaceAccess, get_workspace_access
from app.api.routes import crm_task_executor
from app.api.routes.crm_task_executor import router
from app.database.base import Base
from app.database.session import get_db
from app.models.crm_task import CRMTask


def _database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CRMTask.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    workspace_id = uuid4()
    other_workspace_id = uuid4()
    task_id = uuid4()
    other_task_id = uuid4()
    due_at = datetime.now(UTC) + timedelta(hours=2)
    with factory() as db:
        db.add_all(
            [
                CRMTask(
                    id=task_id,
                    workspace_id=workspace_id,
                    patient_id=uuid4(),
                    task_type="follow_up",
                    source="manual",
                    status="pending",
                    execution_mode="ai",
                    priority="normal",
                    title="Current workspace follow-up",
                    due_at=due_at,
                ),
                CRMTask(
                    id=other_task_id,
                    workspace_id=other_workspace_id,
                    patient_id=uuid4(),
                    task_type="follow_up",
                    source="manual",
                    status="pending",
                    execution_mode="ai",
                    priority="normal",
                    title="Other workspace follow-up",
                    due_at=due_at,
                ),
            ]
        )
        db.commit()

    return engine, factory, workspace_id, task_id, other_task_id


def _client(factory, *, workspace_id, actor_id, role: str) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/crm")
    access = WorkspaceAccess(
        user=SimpleNamespace(id=actor_id),
        workspace=SimpleNamespace(id=workspace_id),
        membership=SimpleNamespace(
            role=role,
            section_access={"clinic_operations": True, "administration": True},
        ),
    )
    app.dependency_overrides[get_workspace_access] = lambda: access

    def test_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = test_get_db
    return TestClient(app)


def _task_state(factory, task_id):
    with factory() as db:
        task = db.get(CRMTask, task_id)
        assert task is not None
        return task.execution_mode, task.assigned_user_id, task.status


@pytest.mark.parametrize(
    "payload",
    [
        {"executor": "unassigned"},
        {"executor": "staff", "assigned_user_id": str(uuid4())},
        {"executor": "tia"},
    ],
)
def test_member_cannot_change_executor_even_with_both_sections_and_state_is_unchanged(monkeypatch, payload) -> None:
    monkeypatch.setattr(crm_task_executor, "_follow_up_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(crm_task_executor, "record_activity_event", lambda *_args, **_kwargs: None)
    engine, factory, workspace_id, task_id, _other_task_id = _database()
    before = _task_state(factory, task_id)
    client = _client(
        factory,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        role="member",
    )
    try:
        with client:
            response = client.patch(
                f"/crm/tasks/{task_id}/executor",
                json=payload,
            )
        assert response.status_code == 403
        assert _task_state(factory, task_id) == before
    finally:
        engine.dispose()


def test_admin_can_change_executor_and_cannot_mutate_another_workspace_task(monkeypatch) -> None:
    monkeypatch.setattr(crm_task_executor, "_follow_up_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(crm_task_executor, "record_activity_event", lambda *_args, **_kwargs: None)
    engine, factory, workspace_id, task_id, other_task_id = _database()
    client = _client(
        factory,
        workspace_id=workspace_id,
        actor_id=uuid4(),
        role="admin",
    )
    try:
        with client:
            updated = client.patch(
                f"/crm/tasks/{task_id}/executor",
                json={"executor": "unassigned"},
            )
            foreign = client.patch(
                f"/crm/tasks/{other_task_id}/executor",
                json={"executor": "unassigned"},
            )
        assert updated.status_code == 200
        assert updated.json() == {
            "task_id": str(task_id),
            "execution_mode": "human",
            "assigned_user_id": None,
        }
        assert _task_state(factory, task_id) == ("human", None, "pending")
        assert foreign.status_code == 404
        assert _task_state(factory, other_task_id) == ("ai", None, "pending")
    finally:
        engine.dispose()
