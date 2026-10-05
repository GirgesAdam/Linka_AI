import Link from "next/link";
import { CircleAlert, Clock3, MessageCircleMore, UserRound, Workflow } from "lucide-react";

import { AutomationTimingForm } from "@/components/automation-timing-form";
import { EmptyState } from "@/components/empty-state";
import { PageHeader } from "@/components/page-header";
import { SubmitButton } from "@/components/submit-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime } from "@/lib/format";
import { labelForStatus, toneForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import type {
  AutomationJob,
  AutomationOperationsOverview,
  AutomationRule,
  ChannelConnection,
} from "@/lib/types";
import {
  cancelAutomationJob,
  retryAutomationJob,
  resumeWhatsappConnection,
  toggleAutomation,
} from "./actions";
import type { WhatsAppSetupState } from "./whatsapp-direct-onboarding";

const names: Record<string, string> = {
  booking_confirmation: "تأكيد الحجز",
  appointment_reminder_6h: "تذكير قبل الموعد",
  post_visit_followup: "متابعة بعد الزيارة",
  cancellation_recovery: "استرجاع الحجوزات الملغاة",
  lead_not_booked_followup: "متابعة العميل اللي ماحجزش",
};

const descriptions: Record<string, string> = {
  booking_confirmation: "ترسل رسالة تأكيد تلقائيًا بعد تسجيل الحجز.",
  appointment_reminder_6h: "تذكّر العميل بالموعد في التوقيت الذي تحدده العيادة.",
  post_visit_followup: "رسالة واحدة للاطمئنان، عرض المساعدة أو حجز الجلسة التالية، وطلب التقييم.",
  cancellation_recovery: "تتواصل مع العميل بعد إلغاء الموعد وتعرض عليه ترتيب موعد جديد.",
  lead_not_booked_followup: "تتابع العميل المهتم لو لسه ماحجزش وتساعده يكمل الحجز.",
};

const recipients: Record<string, string> = {
  appointment_reminder_6h: "العميل صاحب الموعد",
  post_visit_followup: "العميل بعد الزيارة",
  cancellation_recovery: "العميل صاحب الحجز الملغي",
  lead_not_booked_followup: "العميل المهتم الذي لم يحجز",
};

const visibleProductRuleKeys = new Set([
  "appointment_reminder_6h",
  "post_visit_followup",
  "cancellation_recovery",
  "lead_not_booked_followup",
]);

const timingRuleKeys = new Set([
  "appointment_reminder_6h",
  "post_visit_followup",
  "cancellation_recovery",
  "lead_not_booked_followup",
]);

function attentionLabel(job: AutomationJob): string | null {
  if (job.attention_reason === "execution_failed") return "لم تكتمل العملية تلقائيًا";
  if (job.attention_reason === "delivery_failed") return "تعذر إرسال الرسالة";
  if (job.attention_reason === "stuck_processing") return "استغرق التنفيذ وقتًا أطول من المعتاد";
  return null;
}

function jobKindLabel(job: AutomationJob) {
  return job.job_kind === "crm_follow_up" ? "متابعة عميل" : "رسالة مرتبطة بموعد";
}

function timingParts(rule: AutomationRule): {
  value: number;
  unit: "minutes" | "hours" | "days";
} {
  const minutes = Math.abs(rule.offset_minutes);
  if (minutes >= 1440 && minutes % 1440 === 0) return { value: minutes / 1440, unit: "days" };
  if (minutes >= 60 && minutes % 60 === 0) return { value: minutes / 60, unit: "hours" };
  return { value: minutes, unit: "minutes" };
}

function timingLabel(rule: AutomationRule): string {
  if (rule.trigger_kind === "before_appointment") return "أرسل قبل الموعد بـ";
  if (rule.trigger_kind === "after_completed") return "أرسل بعد انتهاء الزيارة بـ";
  if (rule.trigger_kind === "after_no_show") return "أرسل بعد عدم الحضور بـ";
  if (rule.trigger_kind === "after_cancelled") return "أرسل بعد إلغاء الموعد بـ";
  if (rule.trigger_kind === "after_lead_activity") return "أرسل بعد آخر تواصل بـ";
  return "التوقيت";
}

function triggerLabel(rule: AutomationRule) {
  if (rule.trigger_kind === "before_appointment") return "قبل الموعد";
  if (rule.trigger_kind === "after_completed") return "بعد انتهاء الزيارة";
  if (rule.trigger_kind === "after_no_show") return "بعد عدم الحضور";
  if (rule.trigger_kind === "after_cancelled") return "بعد إلغاء الموعد";
  if (rule.trigger_kind === "after_lead_activity") return "بعد آخر تواصل مع العميل";
  return "عند تحقق شرط المتابعة";
}

function automationWarning(state: AutomationOperationsOverview["worker_state"]) {
  if (state === "stale") return "محرك الرسائل التلقائية غير متصل حاليًا، لذلك لن تُرسل الرسائل حتى يعود الاتصال.";
  if (state === "missing") return "محرك الرسائل التلقائية غير مجهز بعد، لذلك لن تُرسل الرسائل حتى يكتمل إعداد التشغيل.";
  return null;
}

function providerHealth(connection: ChannelConnection) {
  const raw = connection.config_json?.provider_health;
  return raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
}

export default async function AutomationsPage() {
  const ctx = await getAppContext();
  const results = await Promise.allSettled([
    tiaRequest<AutomationRule[]>("/automations/rules"),
    tiaRequest<AutomationJob[]>("/automations/jobs?limit=50"),
    tiaRequest<AutomationOperationsOverview>("/automations/overview"),
    tiaRequest<ChannelConnection[]>("/channels/connections"),
    ctx.workspace.role === "admin"
      ? tiaRequest<WhatsAppSetupState>("/channels/whatsapp/setup")
      : Promise.resolve(null),
  ]);

  const [rulesResult, jobsResult, overviewResult, connectionsResult, setupResult] = results;
  if (rulesResult.status === "rejected") throw rulesResult.reason;

  const rules = rulesResult.value.filter((rule) => visibleProductRuleKeys.has(rule.key));
  const jobs = jobsResult.status === "fulfilled" ? jobsResult.value : [];
  const overview = overviewResult.status === "fulfilled" ? overviewResult.value : null;
  const connections = connectionsResult.status === "fulfilled" ? connectionsResult.value : [];
  const whatsappSetup = setupResult.status === "fulfilled" ? setupResult.value : null;
  const operationsUnavailable = jobsResult.status === "rejected" || overviewResult.status === "rejected";
  const whatsappStateUnavailable = ctx.workspace.role === "admin" && setupResult.status === "rejected";

  const attentionJobs = jobs.filter((job) => Boolean(attentionLabel(job)));
  const recentJobs = jobs.slice(0, 12);
  const warning = overview ? automationWarning(overview.worker_state) : null;
  const whatsappConnections = connections.filter(
    (connection) => connection.channel === "whatsapp" && connection.status !== "disconnected",
  );
  const templateStatusByName = new Map(
    (whatsappSetup?.templates || []).map((template) => [template.name, template.status.toLowerCase()]),
  );
  const whatsappAttention = whatsappConnections.filter((connection) => {
    const health = providerHealth(connection);
    const healthState = typeof health?.state === "string" ? health.state : null;
    if (healthState === "setup_pending") return false;
    return connection.status === "paused" || healthState === "degraded" || healthState === "disabled";
  });

  return (
    <>
      <PageHeader
        title="الرسائل التلقائية"
        description="اختار المتابعات التي تحتاجها العيادة، وحدد متى تُرسل ولمن، وتابع حالتها من مكان واحد."
      />

      {ctx.workspace.role === "admin" && (
        <Card id="whatsapp-templates" className={`mb-6 scroll-mt-24 ${whatsappSetup?.ready_for_automations ? "border-emerald-200 bg-emerald-50/40" : "border-amber-200 bg-amber-50/40"}`}>
          <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <span className={`grid size-10 shrink-0 place-items-center rounded-xl text-white ${whatsappSetup?.ready_for_automations ? "bg-emerald-600" : "bg-amber-600"}`}>
                <MessageCircleMore size={19} />
              </span>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <b className="text-slate-950">WhatsApp</b>
                  <Badge tone={whatsappSetup?.ready_for_automations ? "green" : "yellow"}>
                    {whatsappStateUnavailable ? "تعذر التحقق" : whatsappSetup?.ready_for_automations ? "جاهز" : "يحتاج استكمال"}
                  </Badge>
                </div>
                <p className="mt-1 text-sm leading-6 text-[var(--muted)]">
                  {whatsappStateUnavailable
                    ? "تعذر قراءة حالة الربط مؤقتًا. إعداد واتساب نفسه موجود في صفحة مستقلة ولن يمنعك من مراجعة القواعد هنا."
                    : whatsappSetup?.ready_for_automations
                      ? `${whatsappSetup.verified_name || whatsappSetup.display_phone_number || "رقم العيادة"} متصل وجاهز لإرسال الرسائل المفعّلة.`
                      : "كمّل ربط واتساب مرة واحدة قبل تشغيل الرسائل المفعّلة فعليًا."}
                </p>
                <div role="note" className="mt-3 rounded-xl border border-amber-200 bg-amber-50/80 p-3 text-xs text-amber-950">
                  <b className="block">تنبيه رسائل WhatsApp التلقائية</b>
                  <p className="mt-1 leading-5">
                    لو مرّ أكثر من 24 ساعة على آخر رسالة من العميل، سياسات WhatsApp من Meta تسمح بالإرسال التلقائي فقط باستخدام Template معتمد. الرسائل التلقائية في Linka تعتمد على الـTemplates المعتمدة، مش على نص حر تكتبه Linka وقت الإرسال.
                  </p>
                </div>
              </div>
            </div>
            <Link href="/setup/whatsapp" className="inline-flex min-h-10 shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-white px-4 text-sm font-bold text-slate-800 transition hover:bg-slate-50">
              {whatsappSetup?.ready_for_automations ? "إدارة الربط" : "كمّل ربط واتساب"}
            </Link>
          </CardContent>
        </Card>
      )}

      {operationsUnavailable && (
        <div role="status" className="mb-5 flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">
          <CircleAlert size={18} className="mt-0.5 shrink-0" />
          <div>
            <b>بيانات التشغيل اللحظية غير متاحة مؤقتًا</b>
            <p className="mt-1 leading-6">تقدر تراجع القواعد وتعديلها، لكن سجل التنفيذ وبعض الأرقام قد لا تكون محدثة الآن.</p>
          </div>
        </div>
      )}

      {warning && (
        <div className="mb-5 flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <CircleAlert size={18} className="mt-0.5 shrink-0" />
          <div>
            <b>تحتاج مراجعة</b>
            <p className="mt-1 leading-6">{warning}</p>
            {overview?.worker_last_seen_at && <p className="mt-1 text-xs">آخر اتصال بمحرك التنفيذ: {formatDateTime(overview.worker_last_seen_at)}</p>}
          </div>
        </div>
      )}

      {whatsappAttention.map((connection) => {
        const health = providerHealth(connection);
        const lastDelivery = typeof health?.last_delivery_at === "string" ? health.last_delivery_at : null;
        return (
          <div key={connection.id} className="mb-4 flex flex-col gap-4 rounded-2xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-950 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <b>{connection.display_name || "WhatsApp"}</b>
                <Badge tone="red">{connection.status === "paused" ? "متوقف تلقائيًا" : "يحتاج مراجعة"}</Badge>
              </div>
              <p className="mt-2 leading-6">الإرسال التلقائي متوقف مؤقتًا لحماية العيادة من محاولات إرسال غير مفيدة. افتح إعداد واتساب لمعرفة الخطوة المطلوبة.</p>
              {lastDelivery && <p className="mt-1 text-xs">آخر تسليم ناجح: {formatDateTime(lastDelivery)}</p>}
            </div>
            <div className="flex flex-wrap gap-2">
              <Link href="/setup/whatsapp" className="inline-flex h-8 items-center rounded-lg border border-rose-200 bg-white px-3 text-xs font-bold">مراجعة واتساب</Link>
              {connection.status === "paused" && (
                <form action={resumeWhatsappConnection}>
                  <input type="hidden" name="connection_id" value={connection.id} />
                  <SubmitButton type="submit" size="sm" variant="outline" pendingLabel="جارٍ التفعيل...">إعادة التفعيل</SubmitButton>
                </form>
              )}
            </div>
          </div>
        );
      })}

      {overview && (
        <div className="mb-6 grid gap-3 sm:grid-cols-3">
          <Card><CardContent className="p-4"><div className="text-xs font-semibold text-[var(--muted)]">القواعد المفعّلة</div><div className="mt-1 text-2xl font-black text-slate-950">{overview.enabled_rules}</div></CardContent></Card>
          <Card><CardContent className="p-4"><div className="text-xs font-semibold text-[var(--muted)]">تحتاج تدخلًا</div><div className="mt-1 text-2xl font-black text-slate-950">{overview.attention_count}</div><div className="mt-1 text-[11px] text-[var(--muted)]">تظهر التفاصيل فقط عند وجود مشكلة</div></CardContent></Card>
          <Card><CardContent className="p-4"><div className="text-xs font-semibold text-[var(--muted)]">التنفيذ التالي</div><div className="mt-1 font-bold text-slate-900">{overview.next_job_at ? formatDateTime(overview.next_job_at) : "لا يوجد إجراء قريب"}</div></CardContent></Card>
        </div>
      )}

      <div className="mb-3">
        <h2 className="text-lg font-black text-slate-950">المتابعات المتاحة</h2>
        <p className="mt-1 text-sm text-[var(--muted)]">كل متابعة مستقلة. فعّل فقط ما تحتاجه العيادة، ويمكنك تغيير التوقيت في أي وقت.</p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {rules.map((rule) => {
          const timing = timingParts(rule);
          const hasTiming = timingRuleKeys.has(rule.key);
          const templateStatus = templateStatusByName.get(rule.template_name);
          const confirmedRunning = Boolean(rule.enabled && whatsappSetup?.ready_for_automations && templateStatus === "approved");
          const waitingForSetup = Boolean(rule.enabled && ctx.workspace.role === "admin" && whatsappSetup && !confirmedRunning);
          const statusLabel = !rule.enabled ? "متوقفة" : confirmedRunning ? "شغالة" : waitingForSetup ? "في انتظار التجهيز" : "مفعّلة";
          const statusTone = !rule.enabled ? "gray" : confirmedRunning ? "green" : waitingForSetup ? "yellow" : "blue";

          return (
            <Card key={rule.id}>
              <CardContent className="p-5">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <b className="text-slate-950">{names[rule.key] || rule.name}</b>
                      <Badge tone={statusTone}>{statusLabel}</Badge>
                    </div>
                    <p className="mt-2 text-sm leading-6 text-[var(--muted)]">{descriptions[rule.key] || "تنفذ متابعة تلقائيًا عند تحقق شروطها."}</p>
                  </div>
                  {ctx.workspace.role === "admin" && (
                    <form action={toggleAutomation}>
                      <input type="hidden" name="rule_id" value={rule.id} />
                      <input type="hidden" name="enabled" value={String(!rule.enabled)} />
                      <SubmitButton size="sm" variant={rule.enabled ? "outline" : "default"} pendingLabel={rule.enabled ? "جارٍ الإيقاف..." : "جارٍ التفعيل..."}>
                        {rule.enabled ? "إيقاف" : "تفعيل"}
                      </SubmitButton>
                    </form>
                  )}
                </div>

                <div className="mt-4 grid gap-2 rounded-2xl bg-slate-50 p-4 text-xs sm:grid-cols-2">
                  <div><div className="text-[11px] font-bold text-slate-400">تبدأ متى؟</div><div className="mt-1 font-bold text-slate-800">{triggerLabel(rule)}</div></div>
                  <div><div className="text-[11px] font-bold text-slate-400">المستلم</div><div className="mt-1 flex items-center gap-1.5 font-bold text-slate-800"><UserRound size={13} /> {recipients[rule.key] || "العميل المرتبط بالمتابعة"}</div></div>
                  <div><div className="text-[11px] font-bold text-slate-400">القناة</div><div className="mt-1 flex items-center gap-1.5 font-bold text-slate-800"><MessageCircleMore size={13} /> WhatsApp</div></div>
                  <div><div className="text-[11px] font-bold text-slate-400">الرسالة</div><div className="mt-1 font-bold text-slate-800">قالب Linka المعتمد في Meta</div></div>
                </div>

                {ctx.workspace.role === "admin" && hasTiming && (
                  <AutomationTimingForm
                    key={`${rule.id}:${rule.offset_minutes}`}
                    ruleId={rule.id}
                    triggerKind={rule.trigger_kind}
                    label={timingLabel(rule)}
                    initialValue={timing.value}
                    initialUnit={timing.unit}
                  />
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>

      {attentionJobs.length > 0 && (
        <Card className="mt-6 border-amber-200">
          <CardHeader><CardTitle>حالات تحتاج تدخلًا</CardTitle><p className="text-xs leading-5 text-[var(--muted)]">هذه العمليات لم تكتمل تلقائيًا ويمكن للمدير إعادة المحاولة أو إلغاؤها.</p></CardHeader>
          <CardContent className="space-y-3">
            {attentionJobs.map((job) => {
              const attention = attentionLabel(job);
              const canRetry = job.attention_reason === "execution_failed" || job.attention_reason === "delivery_failed";
              const canCancel = job.status === "queued" || job.status === "failed" || (job.status === "dispatched" && job.dispatch_status === "queued");
              return (
                <div key={job.id} className="flex flex-col gap-3 rounded-xl bg-amber-50/70 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div><div className="flex flex-wrap items-center gap-2"><b className="text-sm text-slate-950">{jobKindLabel(job)}</b><Badge tone={toneForStatus(job.status)}>{labelForStatus(job.status)}</Badge></div><p className="mt-1 text-xs font-semibold text-amber-800">{attention}</p><div className="mt-1 text-xs text-[var(--muted)]">{formatDateTime(job.scheduled_for)}</div></div>
                  {ctx.workspace.role === "admin" && (canRetry || canCancel) && (
                    <div className="flex gap-2">
                      {canRetry && <form action={retryAutomationJob}><input type="hidden" name="job_id" value={job.id} /><SubmitButton size="sm" variant="outline" pendingLabel="جارٍ الإعادة...">إعادة المحاولة</SubmitButton></form>}
                      {canCancel && <form action={cancelAutomationJob}><input type="hidden" name="job_id" value={job.id} /><SubmitButton size="sm" variant="ghost" pendingLabel="جارٍ الإلغاء...">إلغاء</SubmitButton></form>}
                    </div>
                  )}
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}

      <details className="mt-6 rounded-2xl border border-[var(--border)] bg-white p-4">
        <summary className="cursor-pointer text-sm font-bold text-slate-800">سجل النشاط الأخير</summary>
        <div className="mt-4">
          {jobsResult.status === "rejected" ? (
            <div role="status" className="rounded-xl bg-amber-50 p-4 text-sm text-amber-950">تعذر تحميل سجل النشاط مؤقتًا. القواعد نفسها ما زالت متاحة للإدارة.</div>
          ) : recentJobs.length ? (
            <div className="divide-y divide-[var(--border)]">
              {recentJobs.map((job) => (
                <div key={job.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between">
                  <div><b className="text-sm">{jobKindLabel(job)}</b><div className="mt-1 text-xs text-[var(--muted)]">{formatDateTime(job.scheduled_for)}</div></div>
                  <Badge tone={toneForStatus(job.status)}>{labelForStatus(job.status)}</Badge>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState icon={Workflow} title="لا يوجد نشاط بعد" description="سيظهر هنا آخر نشاط للرسائل والمتابعات التلقائية." />
          )}
        </div>
      </details>
    </>
  );
}
