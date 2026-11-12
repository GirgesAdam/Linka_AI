from fastapi import FastAPI

from app.api.dependencies.security import get_workspace_access, get_workspace_admin
from app.api.router import api_router


def _route(path: str, method: str):
    app = FastAPI()
    app.include_router(api_router)
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            for context in contexts():
                original = context.original_route
                if context.path == path and method in getattr(original, "methods", set()):
                    return original
        elif getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route
    raise AssertionError(f"Route missing: {method} {path}")


def _section_guards(dependant) -> set[str]:
    found: set[str] = set()
    call = dependant.call
    code = getattr(call, "__code__", None)
    closure = getattr(call, "__closure__", None) or ()
    if code and closure:
        values = {
            name: closure[index].cell_contents
            for index, name in enumerate(code.co_freevars)
        }
        section = values.get("section")
        if section in {"clinic_operations", "administration"}:
            found.add(section)
    for child in dependant.dependencies:
        found |= _section_guards(child)
    return found


def _dependency_calls(dependant) -> set[object]:
    found = {dependant.call}
    for child in dependant.dependencies:
        found |= _dependency_calls(child)
    return found


def test_clinic_operations_and_administration_routes_use_only_section_guards() -> None:
    expected = [
        ("GET", "/clinic/doctor-admin", "clinic_operations"),
        ("POST", "/clinic/services", "clinic_operations"),
        ("POST", "/inventory/products", "clinic_operations"),
        ("POST", "/inventory/items", "clinic_operations"),
        ("PUT", "/inventory/laser-prices", "clinic_operations"),
        ("GET", "/finance/expenses", "administration"),
        ("POST", "/finance/expenses", "administration"),
        ("GET", "/automations/overview", "administration"),
        ("GET", "/clinic/setup-v2", "administration"),
        ("GET", "/operations/activity", "administration"),
        ("GET", "/channels/whatsapp/setup", "administration"),
    ]
    for method, path, section in expected:
        route = _route(path, method)
        assert _section_guards(route.dependant) == {section}, (method, path)


def test_default_access_routes_have_no_section_permission_dependency() -> None:
    expected = [
        ("GET", "/booking/appointments"),
        ("POST", "/booking/appointments"),
        ("POST", "/booking/appointments/{appointment_id}/reschedule"),
        ("GET", "/crm/patients"),
        ("GET", "/crm/patients/lookup"),
        ("POST", "/crm/patients/lookup/batch"),
        ("POST", "/crm/patients"),
        ("GET", "/crm/tasks"),
        ("GET", "/inbox/conversations"),
        ("GET", "/clinic/services"),
        ("GET", "/clinic/doctors"),
        ("GET", "/clinic/branches"),
        ("GET", "/booking/branch-schedules"),
        ("GET", "/clinic/staff"),
        ("GET", "/clinic/knowledge"),
        ("GET", "/clinic/booking-settings"),
        ("GET", "/inventory/products"),
        ("GET", "/inventory/laser-devices"),
        ("GET", "/inventory/laser-prices"),
        ("GET", "/inventory/appointments/{appointment_id}/products"),
        ("GET", "/booking/availability"),
        ("GET", "/auth/workspace/assignees"),
    ]
    for method, path in expected:
        route = _route(path, method)
        assert _section_guards(route.dependant) == set(), (method, path)


def test_appointment_lookup_routes_require_workspace_membership_without_section_grants() -> None:
    for method, path in [
        ("POST", "/booking/appointments"),
        ("POST", "/booking/appointments/{appointment_id}/reschedule"),
        ("GET", "/booking/branch-schedules"),
        ("GET", "/crm/patients/lookup"),
        ("POST", "/crm/patients/lookup/batch"),
    ]:
        route = _route(path, method)
        assert get_workspace_access in _dependency_calls(route.dependant), (method, path)
        assert _section_guards(route.dependant) == set(), (method, path)


def test_team_management_and_section_settings_remain_admin_only() -> None:
    for method, path in [
        ("GET", "/auth/workspace/members"),
        ("POST", "/auth/workspace/invitations"),
        ("PATCH", "/auth/workspace/members/{membership_id}"),
        ("GET", "/auth/workspace/members/{membership_id}/section-access"),
        ("PUT", "/auth/workspace/members/{membership_id}/section-access"),
        ("DELETE", "/auth/workspace/members/{membership_id}"),
    ]:
        route = _route(path, method)
        assert get_workspace_admin in _dependency_calls(route.dependant), (method, path)



def test_crm_task_executor_remains_admin_only() -> None:
    route = _route("/crm/tasks/{task_id}/executor", "PATCH")
    assert get_workspace_admin in _dependency_calls(route.dependant)
