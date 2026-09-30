import Link from "next/link";
import {
  CalendarCheck2,
  CircleAlert,
  ContactRound,
  ListTodo,
  MessageSquareMore,
  Settings2,
  Workflow,
} from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import type { ClinicSetupV2Snapshot } from "@/lib/clinic-setup-v2-types";
import { formatDateTime } from "@/lib/format";
import type { CRMTask, DashboardAppointment, DashboardSummary, HandoffQueueItem } from "@/lib/types";

function AppointmentRow({ appointment }: { appointment: DashboardAppointment }) {
  return (
    <Link
      href={`/appointments/${appointment.id}`}
      className="flex min-h-16 flex-col gap-3 rounded-xl px-3 py-4 transition hover:bg-[var(--surface-2)] sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="min-w-0">
        <div className="break-words font-black text-slate-950">{appointment.patient_name}</div>
        <div className="mt-1 break-words text-xs leading-5 text-[var(--muted)]">
          {appointment.service_name} · {appointment.doctor_name}
        </div>
        <div className="mt-1 text-[11px] text-[var(--muted)]">{appointment.branch_name}</div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs">
        <span className="font-bold text-slate-700">{formatDateTime(appointment.start_at)}</span>
        <StatusBadge domain="appointment" status={appointment.status} showIcon={false} />
      </div>
    </Link>
  );
}

function LocalFailure({
  title,
  description,
  href,
  action,
}: {
  title: string;
  description: string;
  href: string;
  action: string;
}) {
  return (
    <div role="status" className="rounded-xl border border-amber-200 bg-amber-50/70 p-4 text-amber-950">
      <div className="flex items-start gap-3">
        <CircleAlert size={18} className="mt-0.5 shrink-0 text-amber-700" />
        <div className="min-w-0">
          <div className="text-sm font-black">{title}</div>
          <p className="mt-1 text-xs leading-5 text-amber-800">{description}</p>
          <Link href={href} className="mt-3 inline-flex min-h-10 items-center text-xs font-black text-amber-900 underline underline-offset-2">
            {action}
          </Link>
        </div>
      </div>
    </div>
  );
}

export function DashboardWorkspace({
  summary,
  handoffs,
  setup,
  overdueTasks,
  handoffsUnavailable = false,
  setupUnavailable = false,
  overdueTasksUnavailable = false,
}: {
  summary: DashboardSummary;
  handoffs: HandoffQueueItem[];
  setup: ClinicSetupV2Snapshot | null;
  overdueTasks: CRMTask[];
  handoffsUnavailable?: boolean;
  setupUnavailable?: boolean;
  overdueTasksUnavailable?: boolean;
}) {
  const incompleteSetup = Boolean(setup && !setup.readiness.ready);
  const hasActionableAttention =
    incompleteSetup ||
    summary.failed_automation_jobs > 0 ||
    overdueTasks.length > 0 ||
    summary.open_handoffs > 0;

  return (
    <>
      <PageHeader
        title="اليوم في العيادة"
        description="ابدأ بالحاجات اللي محتاجة تدخل، وبعدها راجع مواعيد اليوم وما هو قادم."
      />

      <section aria-labelledby="needs-attention-heading" className="rounded-2xl border border-[var(--border)] bg-white shadow-sm">
        <div className="border-b border-[var(--border)] px-4 py-4 sm:px-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 id="needs-attention-heading" className="text-base font-black text-slate-950">يحتاج انتباه</h2>
              <p className="mt-1 text-xs leading-5 text-[var(--muted)]">راجع كل حالة من مصدرها واتخذ الإجراء المناسب.</p>
            </div>

          </div>
        </div>

        <div className="grid items-start gap-3 p-4 sm:p-5 xl:grid-cols-2">
          {incompleteSetup && setup && (
            <div className="rounded-xl border border-[var(--border)] bg-[var(--interactive-soft)]/45 p-4">
              <div className="flex items-start gap-3">
                <Settings2 size={19} className="mt-0.5 shrink-0 text-[var(--interactive)]" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="text-sm font-black text-slate-950">إعداد العيادة غير مكتمل</div>
                    <span className="text-xs font-black text-[var(--interactive-strong)]">{setup.readiness.progress_percent}%</span>
                  </div>
                  <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
                    كمّل متطلبات الجاهزية عشان الحجز يعتمد على بيانات تشغيل مكتملة.
                  </p>
                  <div className="mt-3 h-2 overflow-hidden rounded-full bg-white" aria-label={`اكتمال إعداد العيادة ${setup.readiness.progress_percent}%`}>
                    <div
                      className="h-full rounded-full bg-[var(--interactive)]"
                      style={{ width: `${Math.max(0, Math.min(100, setup.readiness.progress_percent))}%` }}
                    />
                  </div>
                  {setup.readiness.missing.length > 0 && (
                    <p className="mt-2 break-words text-xs leading-5 text-slate-700">
                      الناقص: {setup.readiness.missing.slice(0, 3).join(" • ")}
                    </p>
                  )}
                  <Link href="/setup" className="mt-3 inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">
                    كمّل الإعداد
                  </Link>
                </div>
              </div>
            </div>
          )}

          {summary.failed_automation_jobs > 0 && (
            <Link
              href="/automations"
              className="rounded-xl border border-red-200 bg-red-50/60 p-4 transition hover:border-red-300"
            >
              <div className="flex items-start gap-3">
                <Workflow size={19} className="mt-0.5 shrink-0 text-red-700" />
                <div className="min-w-0">
                  <div className="text-sm font-black text-red-950">
                    {summary.failed_automation_jobs} عملية أتمتة فشلت
                  </div>
                  <p className="mt-1 text-xs leading-5 text-red-800">
                    قد تكون مرتبطة بموعد أو بمتابعة عميل. افتح الأتمتة لمراجعة سبب الفشل.
                  </p>
                  <span className="mt-3 inline-flex min-h-10 items-center text-xs font-black text-red-900 underline underline-offset-2">
                    مراجعة الأتمتة
                  </span>
                </div>
              </div>
            </Link>
          )}

          {overdueTasks.length > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50/50 p-4">
              <div className="flex items-start gap-3">
                <ListTodo size={19} className="mt-0.5 shrink-0 text-amber-700" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-black text-amber-950">متابعات متأخرة</div>
                  <p className="mt-1 text-xs leading-5 text-amber-800">مهام CRM موعدها فات ولسه حالتها مفتوحة.</p>
                  <div className="mt-3 divide-y divide-amber-200/70">
                    {overdueTasks.slice(0, 3).map((task) => (
                      <Link
                        key={task.id}
                        href={`/tasks?scope=overdue&patient_id=${task.patient_id}`}
                        className="flex min-h-12 items-center justify-between gap-3 py-2.5"
                      >
                        <div className="min-w-0">
                          <div className="break-words text-xs font-black text-amber-950">{task.patient_name}</div>
                          <div className="mt-1 break-words text-[11px] text-amber-800">{task.title} · {formatDateTime(task.due_at)}</div>
                        </div>
                        <StatusBadge domain="priority" status={task.priority} showIcon={false} />
                      </Link>
                    ))}
                  </div>
                  <Link href="/tasks?scope=overdue" className="mt-2 inline-flex min-h-10 items-center text-xs font-black text-amber-900 underline underline-offset-2">
                    فتح كل المتابعات المتأخرة
                  </Link>
                </div>
              </div>
            </div>
          )}
          {handoffs.length > 0 && (
            <div className="rounded-xl border border-[var(--border)] p-4 xl:col-span-2">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2 text-sm font-black text-slate-950">
                    <MessageSquareMore size={18} className="text-[var(--interactive)]" />
                    تدخل بشري في المحادثات
                  </div>
                  <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
                    {summary.open_handoffs} محادثة تحتاج تدخلًا من الفريق أو تم استلامها.
                  </p>
                </div>
                <Link href="/inbox?owner=human" className="inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">
                  فتح Inbox
                </Link>
              </div>
              <div className="mt-3 grid gap-2 lg:grid-cols-2">
                {handoffs.map((handoff) => (
                  <Link
                    key={handoff.id}
                    href={`/inbox/${handoff.conversation_id}`}
                    className="min-w-0 rounded-xl bg-[var(--surface-2)] p-3 transition hover:ring-1 hover:ring-[var(--interactive)]/30"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <b className="min-w-0 break-words text-sm text-slate-950">{handoff.patient_name}</b>
                      <StatusBadge domain="priority" status={handoff.priority} showIcon={false} />
                    </div>
                    <p className="mt-2 break-words text-xs leading-5 text-slate-700">{handoff.reason}</p>
                    <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[var(--muted)]">
                      <span>{handoff.assigned_user_name ? `مع ${handoff.assigned_user_name}` : "غير مسندة لعضو فريق"}</span>
                      {handoff.conversation_unread_count > 0 && <span>{handoff.conversation_unread_count} غير مقروءة</span>}
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          )}

          {!handoffsUnavailable && summary.open_handoffs > 0 && handoffs.length === 0 && (
            <div className="rounded-xl border border-[var(--border)] p-4 xl:col-span-2">
              <div className="flex items-start gap-3">
                <MessageSquareMore size={18} className="mt-0.5 shrink-0 text-[var(--interactive)]" />
                <div className="min-w-0">
                  <div className="text-sm font-black text-slate-950">{summary.open_handoffs} محادثة تحتاج تدخلًا بشريًا</div>
                  <p className="mt-1 text-xs leading-5 text-[var(--muted)]">الملخص يشير إلى تدخل بشري مفتوح، لكن تفاصيل القائمة غير ظاهرة في هذه اللحظة.</p>
                  <Link href="/inbox?owner=human" className="mt-3 inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">فتح Inbox</Link>
                </div>
              </div>
            </div>
          )}

          {setupUnavailable && (
            <LocalFailure
              title="تعذر التحقق من جاهزية إعداد العيادة"
              description="باقي لوحة اليوم تعمل. افتح الإعداد مباشرة إذا كنت تحتاج مراجعة بيانات التشغيل."
              href="/setup"
              action="فتح الإعداد"
            />
          )}
          {overdueTasksUnavailable && (
            <LocalFailure
              title="تعذر تحديث المتابعات المتأخرة"
              description="مواعيد اليوم وباقي المصادر ما زالت متاحة. افتح صفحة المتابعات للمراجعة المباشرة."
              href="/tasks?scope=overdue"
              action="فتح المتابعات"
            />
          )}
          {handoffsUnavailable && (
            <LocalFailure
              title="تعذر تحديث تفاصيل التدخلات البشرية"
              description={summary.open_handoffs > 0 ? `الملخص يشير إلى ${summary.open_handoffs} محادثة تحتاج تدخلًا بشريًا، لكن تفاصيل القائمة غير متاحة الآن.` : "تعذر تحميل قائمة التدخلات البشرية الآن؛ باقي لوحة اليوم تعمل بشكل طبيعي."}
              href="/inbox?owner=human"
              action="فتح Inbox"
            />
          )}

          {!hasActionableAttention && !handoffsUnavailable && !setupUnavailable && !overdueTasksUnavailable && (
            <div className="rounded-xl bg-[var(--surface-2)] p-5 text-sm text-slate-700 xl:col-span-2">
              لا توجد متابعات متأخرة أو تدخلات بشرية أو عمليات أتمتة فاشلة أو متطلبات إعداد عاجلة الآن.
            </div>
          )}
        </div>
      </section>
      <section aria-labelledby="today-heading" className="mt-6">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="today-heading" className="text-lg font-black text-slate-950">اليوم</h2>
            <p className="mt-1 text-xs text-[var(--muted)]">
              {summary.appointments_today} موعد ضمن يوم العيادة، والقائمة تعرض أقرب المواعيد المفتوحة أو الجارية.
            </p>
          </div>
          <Link href="/appointments" className="inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">
            فتح جدول المواعيد
          </Link>
        </div>
        <Card>
          <CardContent className="p-2 sm:p-3">
            {summary.today_appointments.length ? (
              <div className="divide-y divide-[var(--border)]">
                {summary.today_appointments.map((appointment) => (
                  <AppointmentRow key={appointment.id} appointment={appointment} />
                ))}
              </div>
            ) : (
              <EmptyState
                icon={CalendarCheck2}
                title={summary.appointments_today > 0 ? "لا توجد مواعيد مفتوحة أو جارية متبقية اليوم" : "لا توجد مواعيد اليوم"}
                description={summary.appointments_today > 0 ? "قد تكون مواعيد اليوم اكتملت أو انتهت حالتها التشغيلية." : "اليوم خالٍ من الحجوزات حاليًا."}
                action={<Link href="/appointments" className="inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">فتح جدول المواعيد</Link>}
              />
            )}
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="next-heading" className="mt-6">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="next-heading" className="text-lg font-black text-slate-950">القادم بعد اليوم</h2>
            <p className="mt-1 text-xs text-[var(--muted)]">
              {summary.appointments_after_today} موعد مؤكد أو قيد الانتظار بعد نهاية يوم العيادة الحالي.
            </p>
          </div>
          {summary.next_appointments[0] ? (
            <Link href={`/appointments/${summary.next_appointments[0].id}`} className="inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">
              فتح أقرب موعد قادم
            </Link>
          ) : (
            <Link href="/appointments" className="inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">
              فتح جدول المواعيد
            </Link>
          )}
        </div>
        <Card>
          <CardContent className="p-2 sm:p-3">
            {summary.next_appointments.length ? (
              <div className="divide-y divide-[var(--border)]">
                {summary.next_appointments.map((appointment) => (
                  <AppointmentRow key={appointment.id} appointment={appointment} />
                ))}
              </div>
            ) : (
              <EmptyState
                icon={CalendarCheck2}
                title="لا توجد مواعيد بعد اليوم"
                description="ستظهر هنا أقرب المواعيد بعد نهاية يوم العيادة الحالي."
                action={<Link href="/appointments" className="inline-flex min-h-10 items-center text-xs font-black text-[var(--interactive)]">فتح المواعيد</Link>}
              />
            )}
          </CardContent>
        </Card>
      </section>
      <section aria-labelledby="overview-heading" className="mt-6">
        <div className="mb-3">
          <h2 id="overview-heading" className="text-sm font-black text-slate-900">نظرة عامة</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">سياق تشغيلي ثانوي بعد ما تكون الصورة اليومية واضحة.</p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Link href="/patients?status=active" className="rounded-xl border border-[var(--border)] bg-white p-4 transition hover:bg-[var(--surface-2)]">
            <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><ContactRound size={16} /> عملاء بحالة نشط</div>
            <div className="mt-2 text-2xl font-black text-slate-950">{summary.active_patients}</div>
            <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">عدد سجلات المرضى المصنّفة حاليًا كحالة نشطة.</div>
          </Link>
          <Link href="/appointments" className="rounded-xl border border-[var(--border)] bg-white p-4 transition hover:bg-[var(--surface-2)]">
            <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><CalendarCheck2 size={16} /> مواعيد قادمة من الآن</div>
            <div className="mt-2 text-2xl font-black text-slate-950">{summary.upcoming_appointments}</div>
            <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">مواعيد حالتها مؤكدة أو قيد الانتظار من الوقت الحالي فصاعدًا.</div>
          </Link>
        </div>
      </section>
    </>
  );
}
