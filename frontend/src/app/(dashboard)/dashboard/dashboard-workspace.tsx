import Link from "next/link";
import { ArrowUpLeft, CircleAlert, ListTodo, WalletCards } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatMoney } from "@/lib/format";
import type { CRMTask, DashboardToday, DashboardTodayRevenue } from "@/lib/types";
import { TodayAgenda } from "./today-agenda";

function formatClinicDate(localDate: string, timezone: string) {
  const value = localDate ? new Date(`${localDate}T12:00:00Z`) : new Date();
  return new Intl.DateTimeFormat("ar-EG", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: localDate ? "UTC" : timezone,
  }).format(value);
}

function formatTaskTime(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(value));
}

function FollowUpRow({ task, timezone }: { task: CRMTask; timezone: string }) {
  return (
    <Link
      href={`/tasks?scope=today&patient_id=${task.patient_id}`}
      className="group grid min-h-16 gap-3 px-1 py-4 transition hover:bg-slate-50 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-3"
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-[15px] font-semibold text-slate-950">{task.patient_name}</span>
          <StatusBadge domain="priority" status={task.priority} showIcon={false} />
        </div>
        <p className="mt-1 break-words text-[13px] leading-5 text-slate-500">{task.title}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span>{formatTaskTime(task.due_at, timezone)}</span>
          <span aria-hidden="true">·</span>
          <span>{task.status === "in_progress" ? "قيد التنفيذ" : "معلقة"}</span>
        </div>
      </div>
      <span className="inline-flex min-h-11 items-center gap-1 text-xs font-bold text-violet-700">
        فتح المهمة <ArrowUpLeft size={14} />
      </span>
    </Link>
  );
}

function RevenueSection({
  revenue,
  unavailable,
}: {
  revenue: DashboardTodayRevenue | null;
  unavailable: boolean;
}) {
  return (
    <section aria-labelledby="revenue-heading" className="mt-8 sm:mt-10">
      <div className="mb-3">
        <h2 id="revenue-heading" className="text-xl font-semibold text-slate-950">إيرادات اليوم</h2>
        <p className="mt-1 text-sm text-slate-500">المبالغ المحصلة فعليًا خلال اليوم</p>
      </div>

      <Card className="overflow-hidden border-slate-100 shadow-none">
        <CardContent className="p-5 sm:p-7">
          {unavailable || !revenue ? (
            <div className="flex items-start gap-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-950" role="status">
              <CircleAlert size={18} className="mt-0.5 shrink-0" />
              <div>
                <div className="font-semibold">تعذر تحميل إيرادات اليوم مؤقتًا</div>
                <p className="mt-1 text-xs leading-5">المواعيد والمتابعات ما زالت متاحة بشكل طبيعي.</p>
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="text-xs font-semibold text-slate-500">إجمالي اليوم</div>
                  <div className="mt-2 text-3xl font-semibold tracking-tight text-slate-950 sm:text-4xl">
                    {formatMoney(revenue.total_minor, revenue.currency)}
                  </div>
                  {revenue.total_minor === 0 && (
                    <p className="mt-2 text-xs text-slate-500">لم يتم تسجيل تحصيلات اليوم بعد</p>
                  )}
                </div>
                <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-violet-700">
                  <WalletCards size={20} />
                </span>
              </div>

              <div className="mt-6 divide-y divide-slate-100 sm:grid sm:grid-cols-3 sm:divide-x sm:divide-y-0 sm:divide-x-reverse">
                {[
                  ["Cash", revenue.cash_minor],
                  ["Visa", revenue.visa_minor],
                  ["InstaPay", revenue.instapay_minor],
                ].map(([label, amount]) => (
                  <div key={String(label)} className="flex items-center justify-between gap-4 py-3 sm:block sm:px-5 sm:py-1 first:sm:pr-0 last:sm:pl-0">
                    <div className="text-xs font-semibold text-slate-500">{label}</div>
                    <div className="text-sm font-semibold text-slate-900 sm:mt-2">{formatMoney(Number(amount), revenue.currency)}</div>
                  </div>
                ))}
              </div>

              {(revenue.refunds_minor > 0 || revenue.other_minor !== 0) && (
                <div className="mt-5 border-t border-slate-100 pt-4 text-xs leading-5 text-slate-500">
                  {revenue.refunds_minor > 0 && (
                    <div>الإجمالي صافي بعد خصم مرتجعات اليوم: {formatMoney(revenue.refunds_minor, revenue.currency)}</div>
                  )}
                  {revenue.other_minor !== 0 && (
                    <div>طرق دفع قديمة/أخرى ضمن الإجمالي: {formatMoney(revenue.other_minor, revenue.currency)}</div>
                  )}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

export function DashboardWorkspace({
  today,
  followUps,
  revenue,
  followUpsUnavailable = false,
  revenueUnavailable = false,
}: {
  today: DashboardToday;
  followUps: CRMTask[];
  revenue: DashboardTodayRevenue | null;
  followUpsUnavailable?: boolean;
  revenueUnavailable?: boolean;
}) {
  return (
    <main className="mx-auto w-full max-w-[1120px] pb-12">
      <header className="mb-7 sm:mb-9">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-950 sm:text-[28px]">اليوم في العيادة</h1>
        <p className="mt-1 text-sm text-slate-500">{formatClinicDate(today.local_date, today.timezone)}</p>
      </header>

      <section aria-labelledby="appointments-heading">
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 id="appointments-heading" className="text-xl font-semibold text-slate-950">مواعيد اليوم</h2>
            <p className="mt-1 text-sm text-slate-500">اليوم الحالي داخل توقيت العيادة فقط</p>
          </div>
          <Link href="/appointments" className="hidden text-xs font-bold text-violet-700 sm:inline-flex">كل المواعيد</Link>
        </div>
        <TodayAgenda
          appointments={today.appointments}
          timezone={today.timezone}
          nextAppointmentId={today.next_appointment_id}
        />
      </section>

      <section aria-labelledby="followups-heading" className="mt-8 sm:mt-10">
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 id="followups-heading" className="text-xl font-semibold text-slate-950">متابعات اليوم</h2>
            <p className="mt-1 text-sm text-slate-500">المتابعات المستحقة النهارده ولسه ما اتقفلتش</p>
          </div>
          <Link href="/tasks?scope=today" className="hidden text-xs font-bold text-violet-700 sm:inline-flex">كل المتابعات</Link>
        </div>

        <div className="rounded-2xl bg-white px-3 sm:px-4">
          {followUpsUnavailable ? (
            <div className="flex items-start gap-3 py-5 text-sm text-amber-950" role="status">
              <CircleAlert size={18} className="mt-0.5 shrink-0" />
              <div>
                <div className="font-semibold">تعذر تحميل متابعات اليوم مؤقتًا</div>
                <p className="mt-1 text-xs text-slate-500">تقدر تكمل شغلك من المواعيد أو تفتح صفحة المتابعات.</p>
              </div>
            </div>
          ) : followUps.length ? (
            <div className="divide-y divide-slate-100">
              {followUps.map((task) => <FollowUpRow key={task.id} task={task} timezone={today.timezone} />)}
            </div>
          ) : (
            <div className="py-8 text-center">
              <ListTodo size={20} className="mx-auto text-slate-300" />
              <div className="mt-2 text-sm font-semibold text-slate-800">مفيش متابعات معلقة لليوم</div>
            </div>
          )}
        </div>
      </section>

      <RevenueSection revenue={revenue} unavailable={revenueUnavailable} />
    </main>
  );
}
