import Link from "next/link";
import {
  CalendarCheck2,
  CircleAlert,
  ListTodo,
  MessageSquareMore,
  Settings2,
  WalletCards,
  Workflow,
} from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import type { ClinicSetupV2Snapshot } from "@/lib/clinic-setup-v2-types";
import { formatMoney } from "@/lib/format";
import type {
  CRMTask,
  DashboardAppointment,
  DashboardSummary,
  HandoffQueueItem,
} from "@/lib/types";

const COPY = {
  pageTitle: "\u0627\u0644\u064a\u0648\u0645",
  pageDescription:
    "\u0645\u0648\u0627\u0639\u064a\u062f \u0627\u0644\u064a\u0648\u0645\u060c \u0627\u0644\u062a\u062d\u0635\u064a\u0644 \u0627\u0644\u0641\u0639\u0644\u064a\u060c \u0648\u0627\u0644\u062d\u0627\u062c\u0627\u062a \u0627\u0644\u0644\u064a \u0645\u062d\u062a\u0627\u062c\u0629 \u062a\u062f\u062e\u0644 \u062f\u0644\u0648\u0642\u062a\u064a.",
  todayAppointments: "\u0645\u0648\u0627\u0639\u064a\u062f \u0627\u0644\u064a\u0648\u0645",
  sortedByClinicTime: "\u0645\u0631\u062a\u0628\u0629 \u062d\u0633\u0628 \u0627\u0644\u0648\u0642\u062a \u062f\u0627\u062e\u0644 \u064a\u0648\u0645 \u0627\u0644\u0639\u064a\u0627\u062f\u0629",
  allAppointments: "\u0643\u0644 \u0627\u0644\u0645\u0648\u0627\u0639\u064a\u062f",
  todayRevenue: "\u0625\u064a\u0631\u0627\u062f\u0627\u062a \u0627\u0644\u064a\u0648\u0645",
  refundsIncluded: "\u064a\u0634\u0645\u0644 \u062e\u0635\u0645 \u0645\u0631\u062a\u062c\u0639\u0627\u062a \u0627\u0644\u064a\u0648\u0645:",
  otherMethods: "\u0637\u0631\u0642 \u062f\u0641\u0639 \u0642\u062f\u064a\u0645\u0629/\u0623\u062e\u0631\u0649 \u0636\u0645\u0646 \u0627\u0644\u0625\u062c\u0645\u0627\u0644\u064a:",
  todayListTitle: "\u0645\u0648\u0627\u0639\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647",
  todayListDescription: "\u0627\u0644\u0645\u0648\u0627\u0639\u064a\u062f \u062f\u0627\u062e\u0644 \u0627\u0644\u064a\u0648\u0645 \u0627\u0644\u062d\u0627\u0644\u064a \u0644\u0644\u0639\u064a\u0627\u062f\u0629 \u0641\u0642\u0637.",
  noAppointments: "\u0645\u0641\u064a\u0634 \u0645\u0648\u0627\u0639\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0631\u062f\u0647",
  noAppointmentsDescription: "\u0627\u0644\u0635\u0641\u062d\u0629 \u0627\u0644\u0631\u0626\u064a\u0633\u064a\u0629 \u0628\u062a\u0639\u0631\u0636 \u064a\u0648\u0645 \u0627\u0644\u0639\u064a\u0627\u062f\u0629 \u0627\u0644\u062d\u0627\u0644\u064a \u0641\u0642\u0637.",
  openAppointments: "\u0641\u062a\u062d \u0627\u0644\u0645\u0648\u0627\u0639\u064a\u062f",
  attentionTitle: "\u0645\u062d\u062a\u0627\u062c \u062a\u062f\u062e\u0644",
  attentionDescription: "\u0627\u0633\u062a\u062b\u0646\u0627\u0621\u0627\u062a \u062a\u0634\u063a\u064a\u0644\u064a\u0629 \u0628\u0633\u060c \u0645\u0646 \u063a\u064a\u0631 \u0645\u0627 \u062a\u0632\u0627\u062d\u0645 \u0634\u063a\u0644 \u0627\u0644\u064a\u0648\u0645.",
  noAttention: "\u0645\u0641\u064a\u0634 \u062d\u0627\u062c\u0629 \u0645\u062d\u062a\u0627\u062c\u0629 \u062a\u062f\u062e\u0644 \u062d\u0627\u0644\u064a\u064b\u0627.",
  setupIncomplete: "\u0625\u0639\u062f\u0627\u062f \u0627\u0644\u0639\u064a\u0627\u062f\u0629 \u063a\u064a\u0631 \u0645\u0643\u062a\u0645\u0644",
  complete: "\u0645\u0643\u062a\u0645\u0644",
  automationFailed: "Automation \u0641\u0634\u0644",
  overdueFollowup: "\u0645\u062a\u0627\u0628\u0639\u0629 \u0645\u062a\u0623\u062e\u0631\u0629",
  conversationNeedsHuman: "\u0645\u062d\u0627\u062f\u062b\u0629 \u0645\u062d\u062a\u0627\u062c\u0629 \u062a\u062f\u062e\u0644",
  partialUnavailable: "\u0628\u0639\u0636 \u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0645\u062a\u0627\u0628\u0639\u0629 \u063a\u064a\u0631 \u0645\u062a\u0627\u062d\u0629 \u0645\u0624\u0642\u062a\u064b\u0627",
} as const;

function AppointmentRow({
  appointment,
  timezone,
}: {
  appointment: DashboardAppointment;
  timezone: string;
}) {
  const time = new Intl.DateTimeFormat("ar-EG", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(appointment.start_at));

  return (
    <Link
      href={`/appointments/${appointment.id}`}
      className="grid min-h-16 gap-3 rounded-xl px-3 py-4 transition hover:bg-[var(--surface-2)] sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
    >
      <div className="min-w-0">
        <div className="break-words font-black text-slate-950">{appointment.patient_name}</div>
        <div className="mt-1 break-words text-xs leading-5 text-[var(--muted)]">
          {appointment.service_name} - {appointment.doctor_name}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-bold text-slate-700">{time}</span>
        <StatusBadge domain="appointment" status={appointment.status} showIcon={false} />
      </div>
    </Link>
  );
}

function RevenueMethod({ label, amount, currency }: { label: string; amount: number; currency: string }) {
  return (
    <div className="rounded-xl bg-[var(--surface-2)] p-3">
      <div className="text-[11px] font-bold text-[var(--muted)]">{label}</div>
      <div className="mt-1 text-sm font-black text-slate-950">{formatMoney(amount, currency)}</div>
    </div>
  );
}

export function DashboardWorkspace({
  summary,
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
  const needsAttention =
    incompleteSetup ||
    summary.failed_automation_jobs > 0 ||
    overdueTasks.length > 0 ||
    summary.open_handoffs > 0 ||
    handoffsUnavailable ||
    setupUnavailable ||
    overdueTasksUnavailable;
  const revenue = summary.today_revenue;

  return (
    <>
      <PageHeader title={COPY.pageTitle} description={COPY.pageDescription} />

      <section className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
        <Card>
          <CardContent className="p-5 sm:p-6">
            <div className="text-xs font-bold text-[var(--muted)]">{COPY.todayAppointments}</div>
            <div className="mt-2 text-4xl font-black text-slate-950">{summary.appointments_today}</div>
            <div className="mt-4 flex items-center justify-between gap-3">
              <span className="text-xs text-[var(--muted)]">{COPY.sortedByClinicTime}</span>
              <Link href="/appointments" className="text-xs font-black text-[var(--interactive)]">
                {COPY.allAppointments}
              </Link>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-5 sm:p-6">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-xs font-bold text-[var(--muted)]">{COPY.todayRevenue}</div>
                <div className="mt-2 text-3xl font-black text-slate-950">
                  {formatMoney(revenue.total_minor, revenue.currency)}
                </div>
              </div>
              <WalletCards className="text-[var(--interactive)]" size={22} />
            </div>
            <div className="mt-5 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <RevenueMethod label="Cash" amount={revenue.cash_minor} currency={revenue.currency} />
              <RevenueMethod label="Visa" amount={revenue.visa_minor} currency={revenue.currency} />
              <RevenueMethod label="InstaPay" amount={revenue.instapay_minor} currency={revenue.currency} />
            </div>
            {revenue.refunds_minor > 0 && (
              <div className="mt-3 text-xs text-[var(--muted)]">
                {COPY.refundsIncluded} {formatMoney(revenue.refunds_minor, revenue.currency)}
              </div>
            )}
            {revenue.other_minor !== 0 && (
              <div className="mt-1 text-xs text-[var(--muted)]">
                {COPY.otherMethods} {formatMoney(revenue.other_minor, revenue.currency)}
              </div>
            )}
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="today-heading" className="mt-6">
        <div className="mb-3">
          <h2 id="today-heading" className="text-lg font-black text-slate-950">
            {COPY.todayListTitle}
          </h2>
          <p className="mt-1 text-xs text-[var(--muted)]">{COPY.todayListDescription}</p>
        </div>
        <Card>
          <CardContent className="p-2 sm:p-3">
            {summary.today_appointments.length ? (
              <div className="divide-y divide-[var(--border)]">
                {summary.today_appointments.map((appointment) => (
                  <AppointmentRow
                    key={appointment.id}
                    appointment={appointment}
                    timezone={summary.timezone}
                  />
                ))}
              </div>
            ) : (
              <EmptyState
                icon={CalendarCheck2}
                title={COPY.noAppointments}
                description={COPY.noAppointmentsDescription}
                action={
                  <Link href="/appointments" className="text-xs font-black text-[var(--interactive)]">
                    {COPY.openAppointments}
                  </Link>
                }
              />
            )}
          </CardContent>
        </Card>
      </section>

      <section aria-labelledby="attention-heading" className="mt-6">
        <div className="mb-3">
          <h2 id="attention-heading" className="text-lg font-black text-slate-950">
            {COPY.attentionTitle}
          </h2>
          <p className="mt-1 text-xs text-[var(--muted)]">{COPY.attentionDescription}</p>
        </div>
        {!needsAttention ? (
          <div className="rounded-xl border border-[var(--border)] bg-white p-5 text-sm text-slate-700">
            {COPY.noAttention}
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {incompleteSetup && setup && (
              <Link href="/setup" className="rounded-xl border border-[var(--border)] bg-white p-4">
                <div className="flex gap-3">
                  <Settings2 size={18} className="text-[var(--interactive)]" />
                  <div>
                    <div className="text-sm font-black">{COPY.setupIncomplete}</div>
                    <div className="mt-1 text-xs text-[var(--muted)]">
                      {setup.readiness.progress_percent}% {COPY.complete}
                    </div>
                  </div>
                </div>
              </Link>
            )}
            {summary.failed_automation_jobs > 0 && (
              <Link href="/automations" className="rounded-xl border border-red-200 bg-red-50/60 p-4">
                <div className="flex gap-3">
                  <Workflow size={18} />
                  <div className="text-sm font-black">
                    {summary.failed_automation_jobs} {COPY.automationFailed}
                  </div>
                </div>
              </Link>
            )}
            {overdueTasks.length > 0 && (
              <Link href="/tasks?scope=overdue" className="rounded-xl border border-amber-200 bg-amber-50/60 p-4">
                <div className="flex gap-3">
                  <ListTodo size={18} />
                  <div className="text-sm font-black">
                    {overdueTasks.length} {COPY.overdueFollowup}
                  </div>
                </div>
              </Link>
            )}
            {summary.open_handoffs > 0 && (
              <Link href="/inbox?owner=human" className="rounded-xl border border-[var(--border)] bg-white p-4">
                <div className="flex gap-3">
                  <MessageSquareMore size={18} className="text-[var(--interactive)]" />
                  <div className="text-sm font-black">
                    {summary.open_handoffs} {COPY.conversationNeedsHuman}
                  </div>
                </div>
              </Link>
            )}
            {(handoffsUnavailable || setupUnavailable || overdueTasksUnavailable) && (
              <div role="status" className="rounded-xl border border-amber-200 bg-amber-50/70 p-4 text-amber-950">
                <div className="flex gap-3">
                  <CircleAlert size={18} />
                  <div className="text-sm font-black">{COPY.partialUnavailable}</div>
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </>
  );
}
