import Link from "next/link";
import { CheckCircle2, CircleAlert, Clock3, ListTodo, MessageSquareMore, UserRound } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { FilterChip } from "@/components/ui/filter-chip";
import { formatDateTime } from "@/lib/format";
import { labelForStatus, toneForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import type { CRMTask, WorkspaceMember } from "@/lib/types";
import { claimTask, setTaskStatus } from "./actions";
import { ExecutorSelect } from "./executor-select";

type TaskSearchParams = { scope?: string; status?: string; mine?: string; patient_id?: string };
const scopes = [["all", "الكل"], ["overdue", "متأخرة"], ["today", "اليوم"], ["upcoming", "قادمة"]] as const;
const statuses = [["", "كل الحالات"], ["pending", "قيد الانتظار"], ["completed", "مكتملة"], ["cancelled", "ملغاة"]] as const;

function hrefFor(current: TaskSearchParams, key: keyof TaskSearchParams, value: string) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(current)) if (v && k !== key) params.set(k, v);
  if (value) params.set(key, value);
  const query = params.toString();
  return query ? `/tasks?${query}` : "/tasks";
}

export default async function TasksPage({ searchParams }: { searchParams: Promise<TaskSearchParams> }) {
  const raw = await searchParams;
  const filters: TaskSearchParams = {
    scope: scopes.some(([value]) => value === raw.scope) ? raw.scope : "all",
    status: statuses.some(([value]) => value === raw.status) ? raw.status : "",
    mine: raw.mine === "1" ? "1" : "",
    patient_id: (raw.patient_id || "").trim(),
  };
  const ctx = await getAppContext();
  const query = new URLSearchParams({ limit: "100", scope: filters.scope || "all" });
  if (filters.status) query.set("status", filters.status);
  if (filters.mine) query.set("assigned_to_me", "true");
  if (filters.patient_id) query.set("patient_id", filters.patient_id);

  const [tasks, members] = await Promise.all([
    tiaRequest<CRMTask[]>(`/crm/tasks?${query.toString()}`),
    ctx.workspace.role === "admin" ? tiaRequest<WorkspaceMember[]>("/auth/workspace/members") : Promise.resolve([]),
  ]);
  const memberOptions = members
    .filter((member) => member.is_active)
    .map((member) => ({ user_id: member.user_id, label: member.full_name || member.email }));

  const activeCount = tasks.filter((task) => task.status === "pending" || task.status === "in_progress").length;
  const overdueCount = tasks.filter((task) => task.is_overdue).length;

  return (
    <>
      <PageHeader
        title="المتابعات"
        description="حدد موعد المتابعة واختر Linka أو أحد أفراد الفريق من القائمة. Linka ترسل المتابعة تلقائيًا في موعدها."
      />

      {filters.patient_id && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-xs">
          <span className="font-bold text-slate-700">عرض متابعات هذا العميل فقط</span>
          <Link href={hrefFor(filters, "patient_id", "")} className="font-bold text-[var(--interactive)]">عرض كل العملاء</Link>
        </div>
      )}

      <div className="surface-toolbar mb-4">
        {scopes.map(([value, label]) => (
          <FilterChip key={value} href={hrefFor(filters, "scope", value)} active={filters.scope === value}>
            {label}
          </FilterChip>
        ))}
        <span className="mx-1 hidden h-8 w-px bg-slate-200 sm:block" />
        {statuses.map(([value, label]) => (
          <FilterChip key={value || "all"} href={hrefFor(filters, "status", value)} active={filters.status === value}>
            {label}
          </FilterChip>
        ))}
        <FilterChip href={hrefFor(filters, "mine", filters.mine ? "" : "1")} active={Boolean(filters.mine)}>
          متابعتي
        </FilterChip>
        <div className="mr-auto flex gap-2 text-xs text-[var(--muted)]">
          <span>{activeCount} نشطة</span>
          <span>·</span>
          <span>{overdueCount} متأخرة</span>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          {tasks.length ? (
            <div className="divide-y divide-[var(--border)]">
              {tasks.map((task) => {
                const active = task.status === "pending" || task.status === "in_progress";
                const canManage = ctx.workspace.role === "admin" || (task.execution_mode === "human" && task.assigned_user_id === ctx.me.user.id);
                const canClaim = active && (task.execution_mode === "ai" || !task.assigned_user_id);
                const assignee = task.execution_mode === "ai" ? "Linka" : task.assigned_user_name || task.assigned_user_email || "بدون مسؤول";
                const executorValue = task.execution_mode === "ai"
                  ? "tia"
                  : task.assigned_user_id
                    ? `staff:${task.assigned_user_id}`
                    : "unassigned";

                return (
                  <div key={task.id} className="grid gap-4 p-4 sm:p-5 lg:grid-cols-[1.4fr_.8fr_auto] lg:items-center">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <ListTodo size={16} className="text-teal-700" />
                        <b className="text-slate-900">{task.title}</b>
                        <Badge tone={toneForStatus(task.status)}>{labelForStatus(task.status)}</Badge>
                        {task.is_overdue && active && (
                          <Badge tone="red"><CircleAlert size={11} className="ml-1" />متأخرة</Badge>
                        )}
                        {task.execution_mode === "ai" && active && <Badge tone="purple">Linka</Badge>}
                      </div>
                      {task.description && <p className="mt-2 line-clamp-2 text-sm leading-6 text-[var(--muted)]">{task.description}</p>}
                      <div className="mt-2 flex flex-wrap gap-3 text-xs text-[var(--muted)]">
                        <Link href={`/patients/${task.patient_id}`} className="font-bold text-teal-700">{task.patient_name}</Link>
                        {task.conversation_id && (
                          <Link href={`/inbox/${task.conversation_id}`} className="inline-flex items-center gap-1 font-bold text-teal-700">
                            <MessageSquareMore size={12} /> فتح المحادثة
                          </Link>
                        )}
                      </div>
                    </div>

                    <div className="space-y-2 text-sm">
                      <div className={`flex items-center gap-2 ${task.is_overdue && active ? "font-bold text-red-700" : "text-[var(--muted)]"}`}>
                        <Clock3 size={14} /> {formatDateTime(task.due_at)}
                      </div>
                      <div className="flex items-center gap-2 text-[var(--muted)]">
                        <UserRound size={14} /> {assignee}
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center justify-start gap-2 lg:justify-end">
                      {canClaim && (
                        <form action={claimTask}>
                          <input type="hidden" name="task_id" value={task.id} />
                          <input type="hidden" name="patient_id" value={task.patient_id} />
                          <Button size="sm" variant="outline">استلام</Button>
                        </form>
                      )}

                      {task.status === "completed" ? (
                        <span className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-emerald-600 px-3 text-xs font-bold text-white">
                          <CheckCircle2 size={14} /> تم
                        </span>
                      ) : active && canManage ? (
                        <form action={setTaskStatus}>
                          <input type="hidden" name="task_id" value={task.id} />
                          <input type="hidden" name="patient_id" value={task.patient_id} />
                          <input type="hidden" name="status" value="completed" />
                          <Button size="sm" variant="outline"><CheckCircle2 size={14} /> تم</Button>
                        </form>
                      ) : active ? (
                        <span className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-xs font-bold text-slate-400">
                          <CheckCircle2 size={14} /> تم
                        </span>
                      ) : null}

                      {active && canManage && (
                        <form action={setTaskStatus}>
                          <input type="hidden" name="task_id" value={task.id} />
                          <input type="hidden" name="patient_id" value={task.patient_id} />
                          <input type="hidden" name="status" value="cancelled" />
                          <Button size="sm" variant="ghost">إلغاء</Button>
                        </form>
                      )}

                      {ctx.workspace.role === "admin" && active && (
                        <ExecutorSelect
                          taskId={task.id}
                          patientId={task.patient_id}
                          value={executorValue}
                          allowTia={task.status === "pending"}
                          members={memberOptions}
                        />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <EmptyState
              icon={ListTodo}
              title="لا توجد متابعات مطابقة"
              description="غيّر الفلاتر أو اعرض كل المتابعات لرؤية نتائج أخرى."
            />
          )}
        </CardContent>
      </Card>
    </>
  );
}
