import type { MemberSectionAccess, WorkspaceAccess } from "@/lib/types";

export const MEMBER_ACCESS_SECTIONS = [
  "clinic_operations",
  "administration",
] as const;

export type MemberAccessSection = (typeof MEMBER_ACCESS_SECTIONS)[number];
export type ProtectedRouteSection = MemberAccessSection | "admin_only";

function normalizedPath(href: string): string {
  return href.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
}

/** Maps actual application routes to the two manager-controlled sections. */
export function resolveSectionForHref(href: string): ProtectedRouteSection | null {
  const path = normalizedPath(href);

  if (path === "/team" || path.startsWith("/team/")) return "admin_only";

  for (const base of ["/doctors", "/services", "/inventory"]) {
    if (path === base || path.startsWith(base + "/")) return "clinic_operations";
  }

  for (const base of [
    "/analytics",
    "/finance",
    "/automations",
    "/activity",
    "/setup",
    "/knowledge",
    "/channels",
  ]) {
    if (path === base || path.startsWith(base + "/")) return "administration";
  }

  // Dashboard, Inbox, Appointments, Patients, Tasks, demos, onboarding, and
  // any other non-section route are intentionally available by default.
  return null;
}

export function canAccessSection(
  workspace: WorkspaceAccess,
  section: MemberAccessSection,
): boolean {
  if (workspace.role === "admin") return true;
  if (workspace.role !== "member") return false;
  return workspace.section_access?.[section] === true;
}

export function canAccessRoute(
  workspace: WorkspaceAccess | null,
  href: string,
): boolean {
  const required = resolveSectionForHref(href);
  if (required === null) return true;
  if (!workspace) return false;
  if (required === "admin_only") return workspace.role === "admin";
  return canAccessSection(workspace, required);
}

export function emptyMemberSectionAccess(): MemberSectionAccess {
  return { clinic_operations: false, administration: false };
}
