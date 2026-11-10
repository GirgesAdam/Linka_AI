import assert from "node:assert/strict";
import test from "node:test";

import {
  canAccessRoute,
  canAccessSection,
  emptyMemberSectionAccess,
  resolveSectionForHref,
} from "../src/lib/section-access.ts";

const closedMember = {
  role: "member",
  section_access: emptyMemberSectionAccess(),
};

test("real navigation routes map to exactly the two manager-controlled sections", () => {
  for (const route of ["/doctors", "/doctors/123", "/services", "/inventory/items"]) {
    assert.equal(resolveSectionForHref(route), "clinic_operations", route);
  }
  for (const route of [
    "/analytics",
    "/analytics/campaigns",
    "/finance",
    "/automations",
    "/activity",
    "/setup",
    "/setup/whatsapp",
    "/knowledge",
    "/channels",
  ]) {
    assert.equal(resolveSectionForHref(route), "administration", route);
  }
  assert.equal(resolveSectionForHref("/team"), "admin_only");
});

test("default routes remain accessible when both manager-controlled sections are closed", () => {
  for (const route of [
    "/dashboard",
    "/inbox",
    "/inbox/123",
    "/appointments",
    "/appointments/123",
    "/appointments/123/reschedule",
    "/patients",
    "/patients/123",
    "/tasks",
    "/demo",
    "/agent-demo",
    "/onboarding",
    "/a-future-default-route",
  ]) {
    assert.equal(canAccessRoute(closedMember, route), true, route);
  }
});

test("Clinic Operations and Administration flags are independent", () => {
  const clinicOnly = {
    role: "member",
    section_access: { clinic_operations: true, administration: false },
  };
  assert.equal(canAccessSection(clinicOnly, "clinic_operations"), true);
  assert.equal(canAccessSection(clinicOnly, "administration"), false);
  assert.equal(canAccessRoute(clinicOnly, "/doctors"), true);
  assert.equal(canAccessRoute(clinicOnly, "/finance"), false);

  const administrationOnly = {
    role: "member",
    section_access: { clinic_operations: false, administration: true },
  };
  assert.equal(canAccessRoute(administrationOnly, "/inventory"), false);
  assert.equal(canAccessRoute(administrationOnly, "/analytics"), true);
});

test("Team Management remains Admin-only regardless of member section flags", () => {
  const member = {
    role: "member",
    section_access: { clinic_operations: true, administration: true },
  };
  assert.equal(resolveSectionForHref("/team/members/123"), "admin_only");
  for (const memberState of [
    closedMember,
    { role: "member", section_access: { clinic_operations: true, administration: false } },
    { role: "member", section_access: { clinic_operations: false, administration: true } },
    member,
  ]) {
    assert.equal(canAccessRoute(memberState, "/team"), false);
    assert.equal(canAccessRoute(memberState, "/team/members/123"), false);
  }
  const admin = { role: "admin", section_access: emptyMemberSectionAccess() };
  assert.equal(canAccessRoute(admin, "/team"), true);
  assert.equal(canAccessRoute(admin, "/team/members/123"), true);
});

test("Admin bypasses both section flags but does not change default route classification", () => {
  const admin = { role: "admin", section_access: emptyMemberSectionAccess() };
  assert.equal(canAccessRoute(admin, "/doctors"), true);
  assert.equal(canAccessRoute(admin, "/finance"), true);
  assert.equal(canAccessRoute(admin, "/appointments"), true);
});
