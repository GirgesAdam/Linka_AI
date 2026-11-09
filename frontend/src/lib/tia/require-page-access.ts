import "server-only";
import { redirect } from "next/navigation";
import { canAccessPage, type PagePermissionKey } from "@/lib/page-permissions";
import { getAppContext } from "@/lib/tia/workspace";

export async function requirePageAccess(page: PagePermissionKey) {
  const context = await getAppContext();
  if (!canAccessPage(context.workspace, page)) redirect("/forbidden");
  return context;
}
