import Link from "next/link";
import { redirect } from "next/navigation";
import { Bot, History, Settings2, UserRound } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FilterChip } from "@/components/ui/filter-chip";
import { formatDateTime, formatMoney } from "@/lib/format";
import { labelForPriority, labelForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import type { ActivityEvent } from "@/lib/types";

type SearchParams = {
  days?: string;
  actor?: string;
  entity?: string;
};

const periods = [["7", "7 أيام"], ["30", "30 يوم"], ["90", "90 يوم"]] as const;
const actors = [["", "كل المنفذين"], ["staff", "الفريق"], ["ai", "Linka"], ["system", "النظام"]] as const;
const entities = [
  ["", "كل الأنواع"],
  ["appointment", "الحجوزات"],
  ["handoff", "متابعات المحادثات"],
  ["crm_task", "المتابعات"],
  ["automation_job", "عمليات الأتمتة"],
  ["automation_rule", "قواعد الأتمتة"],
  ["automation_worker", "خدمة الأتمتة"],
  ["workspace_member", "الفريق والصلاحيات"],
  ["clinic_knowledge", "بيانات العيادة"],
  ["payment_transaction", "المدفوعات والاستردادات"],
] as const;

const actionLabels: Record<string, string> = {
  "appointment.created": "تم إنشاء حجز",
  "appointment.confirmed": "تم تأكيد حجز",
  "appointment.cancelled": "تم إلغاء حجز",
  "appointment.rescheduled": "تم تغيير موعد حجز",
  "appointment.completed": "تم إنهاء حجز",
  "appointment.no_show": "تم تسجيل عدم حضور",
  "handoff.created": "تم طلب متابعة من الفريق",
  "handoff.escalated": "تم رفع أولوية المتابعة",
  "handoff.claimed": "استلم الفريق المتابعة",
  "handoff.assigned": "تم إسناد المتابعة",
  "handoff.staff_replied": "رد الفريق على المحادثة",
  "handoff.resolved": "تم إنهاء المتابعة",
  "crm_task.created": "تم إنشاء متابعة",
  "crm_task.updated": "تم تعديل متابعة",
  "crm_task.completed": "تم إنهاء متابعة",
  "crm_task.cancelled": "تم إلغاء متابعة",
  "crm_task.claimed": "تم استلام متابعة",
  "automation.rule_updated": "تم تعديل قاعدة أتمتة",
  "automation.job_retried": "تمت إعادة محاولة عملية تلقائية",
  "automation.job_cancelled": "تم إلغاء عملية تلقائية",
  "automation.worker_created": "تم تشغيل خدمة الأتمتة",
  "automation.worker_status_updated": "تم تحديث حالة خدمة الأتمتة",
  "automation.worker_token_rotated": "تم تحديث بيانات أمان خدمة الأتمتة",
  "workspace.member_added": "تمت إضافة عضو للفريق",
  "workspace.member_role_changed": "تم تغيير صلاحية عضو",
  "workspace.member_removed": "تم حذف عضو من العيادة",
  "clinic.knowledge_applied": "تم تطبيق تعديل على بيانات العيادة",
  "payment.recorded": "تم تسجيل دفعة",
  "payment.refunded": "تم تسجيل استرداد",
};

function buildHref(days: string, actor: string, entity: string) {
  const params = new URLSearchParams({ days });
  if (actor) params.set("actor", actor);
  if (entity) params.set("entity", entity);
  return `/activity?${params.toString()}`;
}

function detailText(event: ActivityEvent) {
  const metadata = event.metadata || {};
  const parts: string[] = [];
  if (typeof metadata.from_status === "string" && typeof metadata.to_status === "string") {
    parts.push(`${labelForStatus(metadata.from_status)} → ${labelForStatus(metadata.to_status)}`);
  } else if (typeof metadata.to_status === "string") {
    parts.push(`الحالة: ${labelForStatus(metadata.to_status)}`);
  }
  const roleLabels: Record<string, string> = { admin: "مدير", member: "عضو فريق" };
  if (typeof metadata.from_role === "string" && typeof metadata.to_role === "string") {
    parts.push(`${roleLabels[metadata.from_role] || "صلاحية سابقة"} → ${roleLabels[metadata.to_role] || "صلاحية جديدة"}`);
  } else if (typeof metadata.role === "string") {
    parts.push(`الصلاحية: ${roleLabels[metadata.role] || "عضو فريق"}`);
  }
  if (typeof metadata.priority === "string") parts.push(`الأولوية: ${labelForPriority(metadata.priority)}`);
  if (typeof metadata.amount_minor === "number" && typeof metadata.currency === "string") {
    parts.push(formatMoney(metadata.amount_minor, metadata.currency));
  }
  return parts.join(" · ");
}

const entityLabels: Record<string, string> = {
  appointment: "حجز",
  handoff: "متابعة محادثة",
  crm_task: "متابعة",
  automation_job: "عملية تلقائية",
  automation_rule: "قاعدة أتمتة",
  automation_worker: "خدمة الأتمتة",
  workspace_member: "عضو فريق",
  clinic_knowledge: "بيانات العيادة",
  payment_transaction: "دفعة",
};

function actorTypeLabel(actorType: string) {
  if (actorType === "ai") return "Linka";
  if (actorType === "system") return "النظام";
  return "الفريق";
}

function eventHref(event: ActivityEvent) {
  if (event.entity_type === "appointment" && event.entity_id) return `/appointments/${event.entity_id}`;
  if (event.entity_type === "crm_task") return "/tasks";
  if (event.entity_type.startsWith("automation_")) return "/automations";
  if (event.entity_type === "workspace_member") return "/team";
  if (event.entity_type === "clinic_knowledge") return "/knowledge";
  const appointmentId = event.metadata?.appointment_id;
  if (event.entity_type === "payment_transaction" && typeof appointmentId === "string") return `/appointments/${appointmentId}`;
  const conversationId = event.metadata?.conversation_id;
  if (event.entity_type === "handoff" && typeof conversationId === "string") return `/inbox/${conversationId}`;
  return null;
}

export default async function ActivityPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const ctx = await getAppContext();
  if (ctx.workspace.role !== "admin") redirect("/dashboard");

  const raw = await searchParams;
  const days = periods.some(([value]) => value === raw.days) ? raw.days! : "7";
  const actor = actors.some(([value]) => value === raw.actor) ? raw.actor || "" : "";
  const entity = entities.some(([value]) => value === raw.entity) ? raw.entity || "" : "";
  const query = new URLSearchParams({ days, limit: "150" });
  if (actor) query.set("actor_type", actor);
  if (entity) query.set("entity_type", entity);
  const events = await tiaRequest<ActivityEvent[]>(`/operations/activity?${query.toString()}`);

  return <>
    <PageHeader title="سجل النشاط" description="مراجعة التغييرات والعمليات المهمة داخل العيادة. سجل النشاط لا يخزن نصوص رسائل العملاء أو بيانات الاتصال الحساسة." />

    <div className="surface-toolbar mb-5 w-fit max-w-full">
      {periods.map(([value, label]) => <FilterChip key={value} href={buildHref(value, actor, entity)} active={days === value}>{label}</FilterChip>)}
    </div>

    <div className="mb-6 grid gap-3 md:grid-cols-2">
      <Card><CardContent className="pt-5"><div className="mb-2 text-xs font-bold text-[var(--muted)]">المنفذ</div><div className="flex flex-wrap gap-1">{actors.map(([value, label]) => <FilterChip key={label} href={buildHref(days, value, entity)} active={actor === value}>{label}</FilterChip>)}</div></CardContent></Card>
      <Card><CardContent className="pt-5"><div className="mb-2 text-xs font-bold text-[var(--muted)]">نوع العملية</div><div className="flex flex-wrap gap-1">{entities.map(([value, label]) => <FilterChip key={label} href={buildHref(days, actor, value)} active={entity === value}>{label}</FilterChip>)}</div></CardContent></Card>
    </div>

    <Card>
      <CardHeader className="flex-row items-center justify-between"><CardTitle className="flex items-center gap-2"><History size={19} />آخر الأحداث</CardTitle><span className="text-xs text-[var(--muted)]">{events.length} حدث</span></CardHeader>
      <CardContent className="space-y-3">
        {events.map(event => {
          const href = eventHref(event);
          const detail = detailText(event);
          const actorIcon = event.actor_type === "ai" ? <Bot size={16} /> : event.actor_type === "system" ? <Settings2 size={16} /> : <UserRound size={16} />;
          const content = <div className="rounded-xl border border-[var(--border)] bg-white p-4 transition hover:border-[var(--border-strong)] hover:bg-slate-50">
            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2"><span className="font-black">{actionLabels[event.action] || "تم تنفيذ عملية"}</span><Badge tone={event.actor_type === "ai" ? "purple" : event.actor_type === "system" ? "gray" : "green"}>{actorTypeLabel(event.actor_type)}</Badge></div>
                <div className="mt-2 flex items-center gap-2 text-xs text-[var(--muted)]">{actorIcon}<span>{event.actor_label}</span><span>·</span><span>{entityLabels[event.entity_type] || "عملية"}</span></div>
                {detail && <div className="mt-2 text-xs leading-5 text-[var(--muted)]">{detail}</div>}
              </div>
              <div className="shrink-0 text-xs font-semibold text-[var(--muted)]">{formatDateTime(event.created_at)}</div>
            </div>
          </div>;
          return href ? <Link key={event.id} href={href} className="block">{content}</Link> : <div key={event.id}>{content}</div>;
        })}
        {!events.length && <div className="py-14 text-center text-sm text-[var(--muted)]">لا توجد أحداث مطابقة للفلاتر الحالية.</div>}
      </CardContent>
    </Card>
  </>;
}
