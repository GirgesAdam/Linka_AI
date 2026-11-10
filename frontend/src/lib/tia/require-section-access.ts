import { redirect } from "next/navigation";
import { canAccessSection, type MemberAccessSection } from "@/lib/section-access";
import { getAppContext } from "@/lib/tia/workspace";

export async function requireSectionAccess(section: MemberAccessSection) {
  const context = await getAppContext();
  if (!canAccessSection(context.workspace, section)) redirect("/forbidden");
  return context;
}
