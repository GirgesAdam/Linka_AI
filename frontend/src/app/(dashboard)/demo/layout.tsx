import { requirePageAccess } from "@/lib/tia/require-page-access";

export default async function PermissionLayout({ children }: { children: React.ReactNode }) {
  await requirePageAccess("demo.home");
  return children;
}
