"""Vault authorization and refund API error-contract regressions."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from app.api.dependencies.security import get_workspace_admin
from app.api.routes import payments as payment_api
from app.api.routes import vault as vault_api
from app.schemas.payments import RefundCreate
from app.schemas.vault import VaultActivationRequest, VaultFundingRequest
from app.schemas.vault_pending import VaultResolveRequest
from app.services import vault as service


class DB:
    def __init__(self, result=None):
        self.result = result
        self.rollbacks = 0
        self.commits = 0

    def scalar(self, _query):
        return self.result

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_pending_and_main_endpoints_are_admin_only():
    for path in (
        "/pending-payments", "/pending-payments/{pending_id}/resolve",
        "/pending-payments/{pending_id}/electronic-opening-review",
        "/pending-payments/{pending_id}/electronic-retry",
        "/treasury", "/treasury/movements", "/treasury/{kind}/funding",
        "/accounts/activate", "/treasury/bank-deposits",
    ):
        route = next((r for r in vault_api.router.routes if r.path == path), None)
        assert route is not None, path
        assert any(dep.call is get_workspace_admin for dep in route.dependant.dependencies), path


def test_member_cannot_pass_workspace_admin_guard():
    member = SimpleNamespace(
        workspace=SimpleNamespace(id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        membership=SimpleNamespace(role="member"),
    )
    with pytest.raises(HTTPException) as err:
        get_workspace_admin(member)
    assert err.value.status_code == 403


def test_branch_member_uses_existing_section_access_without_grant(monkeypatch):
    ws, branch, actor = uuid4(), uuid4(), uuid4()
    model_branch = SimpleNamespace(id=branch)
    monkeypatch.setattr(service, "_branch", lambda *_args: model_branch)
    member = SimpleNamespace(workspace_id=ws, user_id=actor, is_active=True, role="member",
                             section_access={"clinic_operations": True, "administration": False})
    assert service.allowed_branch(
        DB(), workspace_id=ws, branch_id=branch, actor_user_id=actor, membership=member,
    ) is model_branch
    member.section_access["clinic_operations"] = False
    with pytest.raises(service.VaultError, match="Access denied"):
        service.allowed_branch(
            DB(), workspace_id=ws, branch_id=branch, actor_user_id=actor, membership=member,
        )


def test_manual_activation_endpoint_is_retired_and_grant_endpoints_removed():
    db = DB()
    with pytest.raises(HTTPException) as error:
        vault_api.activate(
            VaultActivationRequest(kind="main_cash", opening_balance_minor=100),
            access=SimpleNamespace(), db=db,
        )
    assert error.value.status_code == 410
    assert db.commits == 0
    assert not any(r.path == "/branch-grants" for r in vault_api.router.routes)


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "100", 1_000_000_000_001])
def test_manual_funding_requires_positive_integer_minor_units(amount):
    with pytest.raises(ValidationError):
        VaultFundingRequest(amount_minor=amount, idempotency_key="funding-1")


def test_branch_cross_workspace_rejected(monkeypatch):
    ws, branch, actor = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(service, "_branch", lambda *_args: SimpleNamespace(id=branch))
    member = SimpleNamespace(workspace_id=uuid4(), user_id=actor, is_active=True, role="member")
    with pytest.raises(service.VaultError, match="Access denied"):
        service.allowed_branch(
            DB(uuid4()), workspace_id=ws, branch_id=branch,
            actor_user_id=actor, membership=member,
        )


def test_admin_resolution_explanation_is_required():
    with pytest.raises(ValidationError):
        VaultResolveRequest(branch_id=uuid4(), explanation="x")
    assert VaultResolveRequest(
        branch_id=uuid4(), explanation="Verified complete allocations"
    ).explanation.startswith("Verified")


def test_cash_refund_409_rolls_back_at_actual_api_handler(monkeypatch):
    db = DB()
    def fail(*_args, **_kwargs):
        raise DBAPIError("INSERT", {}, Exception("VAULT_REFUND_REQUIRES_APPROVED_FUNDING"))
    monkeypatch.setattr(payment_api, "record_refund", fail)
    ws, user = uuid4(), uuid4()
    with pytest.raises(HTTPException) as err:
        payment_api.create_refund(
            appointment_id=uuid4(),
            payload=RefundCreate(
                payment_transaction_id=uuid4(), amount_minor=1000,
                reason="Local synthetic cash refund",
            ),
            access=SimpleNamespace(
                workspace=SimpleNamespace(id=ws), user=SimpleNamespace(id=user),
                membership=SimpleNamespace(),
            ),
            db=db,
            idempotency_key="refund-test-1",
        )
    assert err.value.status_code == 409
    assert "خزنة الريسبشن" in err.value.detail
    assert db.rollbacks == 1
    assert db.commits == 0
