"""Focused direct-API regressions for database-backed binary page guards."""

from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.page_permissions import require_page_permission
from app.api.dependencies.security import get_workspace_access
from app.api.router import api_router
from app.models.workspace_member import WorkspaceMember
from app.services.page_permissions import PAGE_PERMISSION_KEYS


def _workspace_access(*, role="member", page_permissions=None, active=True):
    workspace_id = uuid4()
    user_id = uuid4()
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        workspace=SimpleNamespace(id=workspace_id, is_active=True),
        membership=WorkspaceMember(
            user_id=user_id,
            workspace_id=workspace_id,
            role=role,
            is_active=active,
            page_permissions=page_permissions,
        ),
    )


def _full_matrix(key):
    return {name: ("manage" if name == key else "none") for name in PAGE_PERMISSION_KEYS}


def _probe_client(access, page_key, *, legacy_access="reader"):
    app = FastAPI()

    guard = require_page_permission(page_key, legacy_access=legacy_access)

    @app.get("/guarded")
    def guarded(member: Annotated[object, Depends(guard)]):
        return {"user_id": str(member.user.id)}

    app.dependency_overrides[get_workspace_access] = lambda: access
    return TestClient(app)


def test_direct_member_api_permissions_and_legacy_policy():
    page_key = "patients.detail"
    cases = [
        ("member", _full_matrix(page_key), "reader", 200),
        ("member", _full_matrix("inventory"), "reader", 403),
        ("member", None, "reader", 200),
        ("member", None, "admin", 403),
        ("admin", _full_matrix("inventory"), "admin", 200),
    ]
    for role, permissions, legacy, expected in cases:
        with _probe_client(
            _workspace_access(role=role, page_permissions=permissions),
            page_key,
            legacy_access=legacy,
        ) as client:
            response = client.get("/guarded")
            assert response.status_code == expected, (role, legacy, response.text)


def test_direct_api_rejects_inactive_and_cross_workspace_members():
    for access in (
        _workspace_access(role="admin", active=False),
        _workspace_access(page_permissions=_full_matrix("patients.detail")),
    ):
        if access.membership.is_active:
            access.membership.workspace_id = uuid4()
        with _probe_client(access, "patients.detail") as client:
            assert client.get("/guarded").status_code == 403


def _guard_for_route(path, method):
    app = FastAPI()
    app.include_router(api_router)
    for route in app.routes:
        effective_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_contexts):
            for context in effective_contexts():
                original = context.original_route
                if context.path == path and method in getattr(original, "methods", set()):
                    return original
        elif getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route
    raise AssertionError(f"Route missing: {method} {path}")


def _guard_keys(dependant):
    found = set()
    call = dependant.call
    code = getattr(call, "__code__", None)
    if code and "page_key" in code.co_freevars:
        pos = code.co_freevars.index("page_key")
        found.add(call.__closure__[pos].cell_contents)
    for dep in dependant.dependencies:
        found |= _guard_keys(dep)
    return found


@pytest.mark.parametrize(
    ("method", "path", "key"),
    [
        ("GET", "/dashboard/summary", "dashboard"),
        ("GET", "/crm/patients", "patients.list"),
        ("GET", "/crm/patients/{patient_id}/profile", "patients.detail"),
        ("GET", "/crm/tasks", "tasks"),
        ("POST", "/crm/tasks/{task_id}/claim", "tasks"),
        ("GET", "/inbox/conversations", "inbox.list"),
        ("POST", "/inbox/conversations/{conversation_id}/messages", "inbox.detail"),
        ("GET", "/booking/appointments", "appointments.calendar"),
        ("GET", "/booking/appointments/{appointment_id}", "appointments.detail"),
        ("POST", "/booking/appointments/{appointment_id}/reschedule", "appointments.reschedule"),
        ("GET", "/clinic/doctor-admin", "doctors"),
        ("GET", "/clinic/services", "services"),
        ("GET", "/clinic/setup-v2", "setup"),
        ("POST", "/clinic/setup-v2/services", "setup"),
        ("GET", "/finance/expenses", "finance"),
        ("GET", "/finance/outstanding-balances", "analytics.outstanding_balances"),
        ("GET", "/inventory/products", "inventory"),
        ("POST", "/inventory/products", "inventory"),
        ("PUT", "/inventory/products/{product_id}/quantity", "inventory"),
        ("POST", "/inventory/items", "inventory"),
        ("POST", "/inventory/items/{item_id}/stock", "inventory"),
        ("POST", "/inventory/items/{item_id}/usage", "inventory"),
        ("PUT", "/inventory/laser-prices", "inventory"),
        ("GET", "/automations/overview", "automations"),
        ("GET", "/operations/activity", "activity"),
        ("GET", "/channels/whatsapp/setup", "setup.whatsapp"),
    ],
)
def test_live_route_wiring_exposes_its_page_guard(method, path, key):
    route = _guard_for_route(path, method)
    assert key in _guard_keys(route.dependant), f"{method} {path}: page {key} missing"


@pytest.mark.parametrize(
    ("method", "path", "page"),
    [
        ("GET", "/inventory/products", "inventory"),
        ("GET", "/dashboard/summary", "dashboard"),
        ("GET", "/crm/patients", "patients.list"),
        ("GET", "/crm/tasks", "tasks"),
        ("GET", "/inbox/summary", "inbox.list"),
        ("GET", "/booking/appointments", "appointments.calendar"),
        ("GET", "/finance/expenses", "finance"),
    ],
)
def test_member_none_cannot_bypass_actual_business_route_with_direct_url(method, path, page):
    # A denied dependency must short-circuit before any business DB operation.
    from app.database.session import get_db

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_workspace_access] = lambda: _workspace_access(
        page_permissions=_full_matrix("tasks" if page != "tasks" else "dashboard")
    )
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 403, (method, path, response.text)


def _guard_specs(dependant):
    found = set()
    call = dependant.call
    code = getattr(call, "__code__", None)
    closure = getattr(call, "__closure__", None) or ()
    if code and closure:
        values = {name: closure[index].cell_contents for index, name in enumerate(code.co_freevars)}
        if "page_key" in values and "legacy_access" in values:
            found.add((values["page_key"], values["legacy_access"]))
    for dependency in dependant.dependencies:
        found |= _guard_specs(dependency)
    return found


@pytest.mark.parametrize(
    ("path", "page_key"),
    [
        ("/inventory/products", "inventory"),
        ("/clinic/services", "services"),
        ("/automations/overview", "automations"),
        ("/clinic/setup-v2", "setup"),
    ],
)
def test_legacy_hidden_pages_require_admin_fallback_on_every_page_guard(path, page_key):
    route = _guard_for_route(path, "GET")
    assert (page_key, "admin") in _guard_specs(route.dependant)


def test_legacy_null_member_cannot_directly_call_frontend_hidden_page_apis():
    from app.database.session import get_db

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_workspace_access] = lambda: _workspace_access(
        page_permissions=None
    )
    app.dependency_overrides[get_db] = lambda: None
    paths = (
        "/inventory/products",
        "/clinic/services",
        "/automations/overview",
        "/clinic/setup-v2",
    )
    with TestClient(app) as client:
        for path in paths:
            response = client.get(path)
            assert response.status_code == 403, (path, response.text)
