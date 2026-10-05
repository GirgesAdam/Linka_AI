import type { ClinicSetupV2Snapshot } from "@/lib/clinic-setup-v2-types";
import { tiaRequest } from "@/lib/tia/api";
import type { CRMTask, DashboardSummary, HandoffQueueItem } from "@/lib/types";
import { DashboardWorkspace } from "./dashboard-workspace";

export default async function DashboardPage() {
  const [summaryResult, handoffsResult, setupResult, overdueTasksResult, todayTasksResult] = await Promise.allSettled([
    tiaRequest<DashboardSummary>("/dashboard/summary"),
    tiaRequest<HandoffQueueItem[]>("/inbox/handoffs?limit=5"),
    tiaRequest<ClinicSetupV2Snapshot>("/clinic/setup-v2"),
    tiaRequest<CRMTask[]>("/crm/tasks?scope=overdue&limit=5"),
    tiaRequest<CRMTask[]>("/crm/tasks?scope=today&task_type=follow_up&limit=100"),
  ]);

  if (summaryResult.status === "rejected") throw summaryResult.reason;

  return (
    <DashboardWorkspace
      summary={summaryResult.value}
      handoffs={handoffsResult.status === "fulfilled" ? handoffsResult.value : []}
      setup={setupResult.status === "fulfilled" ? setupResult.value : null}
      overdueTasks={overdueTasksResult.status === "fulfilled" ? overdueTasksResult.value : []}
      todayTasks={todayTasksResult.status === "fulfilled" ? todayTasksResult.value : []}
      handoffsUnavailable={handoffsResult.status === "rejected"}
      setupUnavailable={setupResult.status === "rejected"}
      overdueTasksUnavailable={overdueTasksResult.status === "rejected"}
      todayTasksUnavailable={todayTasksResult.status === "rejected"}
    />
  );
}
