import type { WorkspaceAccess } from "@/lib/types";

export const PAGE_PERMISSION_KEYS = [
  "dashboard",
  "inbox.list",
  "inbox.detail",
  "appointments.calendar",
  "appointments.detail",
  "appointments.reschedule",
  "patients.list",
  "patients.detail",
  "tasks",
  "doctors",
  "services",
  "inventory",
  "analytics.overview",
  "analytics.campaigns",
  "analytics.campaign_detail",
  "analytics.cohort_detail",
  "analytics.outstanding_balances",
  "finance",
  "automations",
  "activity",
  "team",
  "setup",
  "setup.whatsapp",
  "demo.home",
  "demo.agent",
] as const;

export type PagePermissionKey = (typeof PAGE_PERMISSION_KEYS)[number];
export type PagePermissionLevel = "none" | "manage";

const legacyMemberPages = new Set<PagePermissionKey>([
  "dashboard", "inbox.list", "inbox.detail", "appointments.calendar",
  "appointments.detail", "appointments.reschedule", "patients.list",
  "patients.detail", "tasks", "doctors", "analytics.overview",
  "analytics.campaigns", "analytics.campaign_detail",
  "analytics.cohort_detail", "analytics.outstanding_balances",
  "finance", "demo.home", "demo.agent",
]);

function isCompletePermissionMatrix(
  value: unknown,
): value is Record<PagePermissionKey, PagePermissionLevel> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const matrix = value as Record<string, unknown>;
  if (Object.keys(matrix).length !== PAGE_PERMISSION_KEYS.length) return false;
  return PAGE_PERMISSION_KEYS.every((key) => {
    if (!Object.prototype.hasOwnProperty.call(matrix, key)) return false;
    const level = matrix[key];
    if (level !== "none" && level !== "manage") return false;
    return key !== "team" || level === "none";
  });
}

export function canAccessPage(workspace: WorkspaceAccess, key: PagePermissionKey): boolean {
  if (workspace.role === "admin") return true;
  if (key === "team") return false;
  const permissions = workspace.page_permissions;
  if (permissions === null) return legacyMemberPages.has(key);
  if (!isCompletePermissionMatrix(permissions)) return false;
  return permissions[key] === "manage";
}

/** True for Admins and Members explicitly granted the complete page matrix. */
export function canManagePage(workspace: WorkspaceAccess, key: PagePermissionKey): boolean {
  if (workspace.role === "admin") return true;
  if (key === "team" || !isCompletePermissionMatrix(workspace.page_permissions)) return false;
  return workspace.page_permissions[key] === "manage";
}

/** Maps in-app links to the same page keys used by route and API authorization. */
export function resolvePagePermissionForHref(href: string): PagePermissionKey | null {
  const path = href.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";

  if (path === "/dashboard") return "dashboard";
  if (path === "/inbox") return "inbox.list";
  if (/^\/inbox\/[^/]+$/.test(path)) return "inbox.detail";
  if (path === "/appointments") return "appointments.calendar";
  if (/^\/appointments\/[^/]+\/reschedule$/.test(path)) return "appointments.reschedule";
  if (/^\/appointments\/[^/]+$/.test(path)) return "appointments.detail";
  if (path === "/patients") return "patients.list";
  if (/^\/patients\/[^/]+$/.test(path)) return "patients.detail";
  if (path === "/tasks") return "tasks";
  if (path === "/doctors") return "doctors";
  if (path === "/services") return "services";
  if (path === "/inventory") return "inventory";
  if (path === "/analytics") return "analytics.overview";
  if (path === "/analytics/campaigns") return "analytics.campaigns";
  if (/^\/analytics\/campaigns\/[^/]+$/.test(path)) return "analytics.campaign_detail";
  if (/^\/analytics\/cohorts\/[^/]+$/.test(path)) return "analytics.cohort_detail";
  if (path === "/analytics/outstanding-balances") return "analytics.outstanding_balances";
  if (path === "/finance") return "finance";
  if (path === "/automations") return "automations";
  if (path === "/activity") return "activity";
  if (path === "/team" || path === "/onboarding") return "team";
  if (path === "/setup/whatsapp" || path === "/channels") return "setup.whatsapp";
  if (path === "/setup" || path === "/setup/integration" || path === "/knowledge") return "setup";
  if (path === "/demo") return "demo.home";
  if (path === "/agent-demo") return "demo.agent";
  return null;
}

export function emptyMemberPermissions(): Record<PagePermissionKey, PagePermissionLevel> {
  const matrix = Object.fromEntries(PAGE_PERMISSION_KEYS.map((key) => [key, "none"]));
  return matrix as Record<PagePermissionKey, PagePermissionLevel>;
}
