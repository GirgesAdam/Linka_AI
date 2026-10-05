import { tiaRequest } from "@/lib/tia/api";
import type { CRMTask, DashboardSummary, DashboardToday, DashboardTodayRevenue } from "@/lib/types";
import { DashboardWorkspace } from "./dashboard-workspace";

async function loadToday(): Promise<DashboardToday> {
  try {
    return await tiaRequest<DashboardToday>("/dashboard/today");
  } catch {
    const legacy = await tiaRequest<DashboardSummary>("/dashboard/summary");
    return {
      timezone: legacy.timezone || "Africa/Cairo",
      local_date: "",
      appointments: legacy.today_appointments || [],
      next_appointment_id: null,
    };
  }
}

export default async function DashboardPage() {
  const [todayResult, followUpsResult, revenueResult] = await Promise.allSettled([
    loadToday(),
    tiaRequest<CRMTask[]>("/crm/tasks?scope=today&task_type=follow_up&limit=100"),
    tiaRequest<DashboardTodayRevenue>("/dashboard/today-revenue"),
  ]);

  if (todayResult.status === "rejected") throw todayResult.reason;

  return (
    <DashboardWorkspace
      today={todayResult.value}
      followUps={followUpsResult.status === "fulfilled" ? followUpsResult.value : []}
      revenue={revenueResult.status === "fulfilled" ? revenueResult.value : null}
      followUpsUnavailable={followUpsResult.status === "rejected"}
      revenueUnavailable={revenueResult.status === "rejected"}
    />
  );
}
