import Link from "next/link";
import { CalendarCheck2, CircleAlert, ContactRound, MessageSquareMore, Settings2 } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { StatCard } from "@/components/stat-card";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ClinicSetupV2Snapshot } from "@/lib/clinic-setup-v2-types";
import { formatDateTime, formatMoney } from "@/lib/format";
import { appointmentLabels, labelForPriority, toneForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type { DashboardSummary, HandoffQueueItem } from "@/lib/types";

export default async function DashboardPage() {
  const [summaryResult, handoffsResult, setupResult] = await Promise.allSettled([
    tiaRequest<DashboardSummary>("/dashboard/summary"),
    tiaRequest<HandoffQueueItem[]>("/inbox/handoffs?limit=5"),
    tiaRequest<ClinicSetupV2Snapshot>("/clinic/setup-v2"),
  ]);

  if (summaryResult.status === "rejected") throw summaryResult.reason;

  const summary = summaryResult.value;
  const handoffs = handoffsResult.status === "fulfilled" ? handoffsResult.value : [];
  const handoffsUnavailable = handoffsResult.status === "rejected";
  const setup = setupResult.status === "fulfilled" ? setupResult.value : null;
  const hasOperationalIssue = summary.failed_automation_jobs > 0;

  return (
    <>
      <PageHeader
        title="ملخص اليوم"
        description="الحجوزات والمتابعات المهمة في مكان واحد، عشان تعرف بسرعة إيه اللي محتاج تدخل منك أو من الفريق."
      />

      {setup && !setup.readiness.ready && (
        <Card className="mb-5 border-teal-200 bg-teal-50/50">
          <CardContent className="p-5">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 font-black text-slate-950">
                  <Settings2 size={18} className="text-teal-700" />
                  كمّل تجهيز العيادة قبل تشغيل Linka بالكامل
                </div>
                <p className="mt-1 text-sm leading-6 text-[var(--muted)]">
                  البيانات الأساسية مكتملة بنسبة {setup.readiness.progress_percent}%. كمّل الناقص عشان الحجوزات والردود التلقائية تعتمد على بيانات صحيحة.
                </p>
                <div className="mt-3 h-2 overflow-hidden rounded-full bg-white" aria-label={`اكتمال إعداد العيادة ${setup.readiness.progress_percent}%`}>
                  <div className="h-full rounded-full bg-teal-700" style={{ width: `${Math.max(0, Math.min(100, setup.readiness.progress_percent))}%` }} />
                </div>
                {setup.readiness.missing.length > 0 && (
                  <p className="mt-2 text-xs leading-5 text-teal-950">الخطوة التالية: {setup.readiness.missing.slice(0, 3).join(" • ")}</p>
                )}
              </div>
              <Link href="/setup" className="inline-flex min-h-10 shrink-0 items-center justify-center rounded-xl bg-teal-700 px-4 text-sm font-bold text-white transition hover:bg-teal-800">
                كمّل الإعداد
              </Link>
            </div>
          </CardContent>
        </Card>
      )}

      {hasOperationalIssue && (
        <Link
          href="/automations"
          className="mb-5 flex items-start gap-3 rounded-2xl border border-amber-200/80 bg-amber-50/80 p-4 text-amber-950 transition hover:border-amber-300 hover:bg-amber-50"
        >
          <CircleAlert className="mt-0.5 shrink-0 text-amber-700" size={19} />
          <div>
            <div className="text-sm font-black">فيه {summary.failed_automation_jobs} رسالة تلقائية محتاجة مراجعة</div>
            <div className="mt-1 text-xs leading-5 text-amber-800">افتح صفحة الرسائل التلقائية لإعادة المحاولة أو مراجعة الحالة.</div>
          </div>
        </Link>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="حجوزات اليوم"
          value={summary.appointments_today}
          detail="كل المواعيد المجدولة اليوم"
          icon={CalendarCheck2}
          href="/appointments?scope=today"
        />
        <StatCard
          label="مواعيد قادمة"
          value={summary.upcoming_appointments}
          detail="المواعيد المؤكدة أو المنتظرة بعد اليوم"
          icon={CalendarCheck2}
          href="/appointments?scope=upcoming"
        />
        <StatCard
          label="تحتاج متابعة"
          value={summary.open_handoffs}
          detail="محادثات تنتظر تدخلًا من الفريق"
          icon={MessageSquareMore}
          href="/inbox?owner=human"
        />
        <StatCard
          label="عملاء نشطون"
          value={summary.active_patients}
          detail="إجمالي العملاء النشطين حاليًا"
          icon={ContactRound}
          href="/patients"
        />
      </div>

      <div className="mt-6 grid gap-5 xl:grid-cols-[1.35fr_.85fr]">
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-3">
            <div>
              <CardTitle>المواعيد القريبة</CardTitle>
              <div className="mt-1 text-xs text-[var(--muted)]">أقرب المواعيد التي يحتاج الفريق أن يكون مستعدًا لها.</div>
            </div>
            <Link href="/appointments" className="shrink-0 text-xs font-bold text-teal-700 hover:text-teal-800">عرض المواعيد</Link>
          </CardHeader>
          <CardContent className="pt-0">
            {summary.recent_appointments.length ? (
              <div className="divide-y divide-[var(--border)]">
                {summary.recent_appointments.map((appointment) => (
                  <Link
                    key={appointment.id}
                    href={`/appointments/${appointment.id}`}
                    className="flex flex-col gap-3 rounded-xl py-4 transition first:pt-1 hover:bg-slate-50 sm:flex-row sm:items-center sm:justify-between sm:px-2"
                  >
                    <div className="min-w-0">
                      <div className="truncate font-bold text-slate-900">{appointment.patient_name}</div>
                      <div className="mt-1 truncate text-xs text-[var(--muted)]">
                        {appointment.service_name} · {appointment.doctor_name}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs">
                      <span className="font-semibold text-slate-700">{formatDateTime(appointment.start_at)}</span>
                      <Badge tone={toneForStatus(appointment.status)}>{appointmentLabels[appointment.status] || "غير محدد"}</Badge>
                      <span className="text-[var(--muted)]">{formatMoney(appointment.price_minor, appointment.currency)}</span>
                    </div>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState
                icon={CalendarCheck2}
                title="لا توجد مواعيد قريبة"
                description="ستظهر هنا أقرب الحجوزات بمجرد وجود مواعيد قادمة."
                action={<Link href="/appointments" className="text-xs font-black text-teal-700 hover:underline">فتح المواعيد</Link>}
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-start justify-between gap-3">
            <div>
              <CardTitle>محادثات تحتاج تدخلًا</CardTitle>
              <div className="mt-1 text-xs text-[var(--muted)]">أولوية للفريق بدل ما تضيع وسط باقي الرسائل.</div>
            </div>
            <Link href="/inbox?owner=human" className="shrink-0 text-xs font-bold text-teal-700 hover:text-teal-800">فتح الرسائل</Link>
          </CardHeader>
          <CardContent className="pt-0">
            {handoffsUnavailable ? (
              <div role="status" className="my-3 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">
                <b>تعذر تحديث قائمة المحادثات الآن.</b>
                <p className="mt-1 text-xs leading-5">باقي الصفحة تعمل بشكل طبيعي. افتح الرسائل أو أعد المحاولة بعد قليل.</p>
                <Link href="/inbox?owner=human" className="mt-3 inline-block text-xs font-black text-amber-900 underline underline-offset-2">فتح الرسائل</Link>
              </div>
            ) : handoffs.length ? (
              <div className="divide-y divide-[var(--border)]">
                {handoffs.map((handoff) => (
                  <Link
                    key={handoff.id}
                    href={`/inbox/${handoff.conversation_id}`}
                    className="block py-4 first:pt-1"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <b className="truncate text-sm text-slate-900">{handoff.patient_name}</b>
                      <Badge tone={toneForStatus(handoff.priority)}>{labelForPriority(handoff.priority)}</Badge>
                    </div>
                    <p className="mt-2 line-clamp-2 text-xs leading-5 text-[var(--muted)]">{handoff.reason}</p>
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState
                icon={MessageSquareMore}
                title="كل المحادثات تحت السيطرة"
                description="مفيش محادثات محتاجة تدخل يدوي من الفريق حاليًا."
              />
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
