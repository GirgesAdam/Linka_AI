import { DashboardShell } from "@/components/dashboard-shell";
import { SectionAccessProvider } from "@/components/permission-link";
import { getAppContext } from "@/lib/tia/workspace";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { me, workspace } = await getAppContext();
  const demoMode = "is_demo" in workspace && workspace.is_demo === true;
  return (
    <SectionAccessProvider workspace={workspace}>
      <DashboardShell me={me} workspace={workspace} demoMode={demoMode}>{children}</DashboardShell>
    </SectionAccessProvider>
  );
}
