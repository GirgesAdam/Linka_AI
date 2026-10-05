import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import type {
  CRMTask,
  DashboardSummary,
  DashboardToday,
  DashboardTodayRevenue,
  InboxConversationListItem,
} from "@/lib/types";
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
  const ctx = await getAppContext();
  const [todayResult, teamMessagesResult, followUpsResult, revenueResult] = await Promise.allSettled([
    loadToday(),
    tiaRequest<InboxConversationListItem[]>("/inbox/conversations?owner_type=human&status=pending&limit=100"),
    tiaRequest<CRMTask[]>("/crm/tasks?scope=due&task_type=follow_up&limit=100"),
    tiaRequest<DashboardTodayRevenue>("/dashboard/today-revenue"),
  ]);

  if (todayResult.status === "rejected") throw todayResult.reason;

  return (
    <DashboardWorkspace
      today={todayResult.value}
      teamMessages={teamMessagesResult.status === "fulfilled" ? teamMessagesResult.value : []}
      followUps={followUpsResult.status === "fulfilled" ? followUpsResult.value : []}
      revenue={revenueResult.status === "fulfilled" ? revenueResult.value : null}
      teamMessagesUnavailable={teamMessagesResult.status === "rejected"}
      followUpsUnavailable={followUpsResult.status === "rejected"}
      revenueUnavailable={revenueResult.status === "rejected"}
      currentUserId={ctx.me.user.id}
      isAdmin={ctx.workspace.role === "admin"}
    />
  );
}
