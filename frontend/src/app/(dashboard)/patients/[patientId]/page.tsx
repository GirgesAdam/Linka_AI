import type { ReactNode } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Bot,
  CalendarCheck2,
  CalendarClock,
  CircleAlert,
  Clock3,
  ContactRound,
  CircleDollarSign,
  ListTodo,
  MessageSquareMore,
  StickyNote,
  Tag,
} from "lucide-react";
import { addPatientNote, createPatientTask } from "../actions";
import { PatientPackagePanel } from "./package-panel";
import { PatientPulsePanel } from "./pulse-panel";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { formatDateTime, formatMoney } from "@/lib/format";
import { appointmentLabels, labelForChannel, labelForSource, labelForStatus } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type { PatientProfile, PatientTimelineEvent } from "@/lib/types";

const noteLabels: Record<string, string> = {
  general: "ملاحظة عامة",
  preference: "تفضيل",
  customer_service: "خدمة عملاء",
  follow_up: "متابعة",
};

const handoffEventLabels: Record<string, string> = {
  created: "تم التصعيد للفريق",
  escalated: "تم رفع أولوية التصعيد",
  claimed: "استلم الفريق المتابعة",
  assigned: "تم إسناد المتابعة",
  staff_replied: "رد الفريق",
  resolved: "تم إنهاء المتابعة",
  reopened: "تم فتح المتابعة من جديد",
};

function actorLabel(event: PatientTimelineEvent) {
  if (event.actor_name) return event.actor_name;
  if (event.actor_type === "patient") return "العميل";
  if (event.actor_type === "ai") return "Linka";
  if (event.actor_type === "staff") return "الفريق";
  return "النظام";
}

function nextOperationalAction(stats: PatientProfile["stats"]) {
  const appointmentAt = stats.next_appointment_at;
  const taskAt = stats.next_task_at;
  if (!appointmentAt && !taskAt) return null;
  if (taskAt && (!appointmentAt || Date.parse(taskAt) <= Date.parse(appointmentAt))) {
    return { kind: "task" as const, at: taskAt };
  }
  return appointmentAt ? { kind: "appointment" as const, at: appointmentAt } : null;
}

function TimelineIcon({ event }: { event: PatientTimelineEvent }) {
  if (event.kind === "message") return event.actor_type === "ai" ? <Bot size={16} /> : <MessageSquareMore size={16} />;
  if (event.kind === "note") return <StickyNote size={16} />;
  if (event.kind === "appointment" || event.kind === "appointment_status") return <CalendarCheck2 size={16} />;
  if (event.kind === "handoff") return <CircleAlert size={16} />;
  if (event.kind === "task") return <ListTodo size={16} />;
  if (event.kind === "payment") return <CircleDollarSign size={16} />;
  return <ContactRound size={16} />;
}

function TimelineEvent({ event, patientId, isLast }: { event: PatientTimelineEvent; patientId: string; isLast: boolean }) {
  const appointment = event.appointment;
  const message = event.message;
  const handoff = event.handoff;
  const note = event.note;
  const task = event.task;
  const payment = event.payment;

  let title = "تم إنشاء ملف العميل";
  let body: ReactNode = null;

  if (note) {
    title = noteLabels[note.note_type] || "ملاحظة";
    body = <p className="whitespace-pre-wrap text-sm leading-6">{note.content}</p>;
  } else if (appointment && event.kind === "appointment") {
    title = `حجز ${appointment.service_name}`;
    body = (
      <div className="space-y-1 text-sm text-[var(--muted)]">
        <div>{formatDateTime(appointment.start_at)} · {appointment.doctor_name}</div>
        <div>{formatMoney(appointment.price_minor, appointment.currency)}</div>
      </div>
    );
  } else if (appointment && event.kind === "appointment_status") {
    const from = appointment.from_status ? appointmentLabels[appointment.from_status] || appointment.from_status : "—";
    const to = appointment.to_status ? appointmentLabels[appointment.to_status] || appointment.to_status : appointment.status;
    title = `تغيير حالة الحجز: ${from} → ${to}`;
    body = (
      <div className="space-y-1 text-sm text-[var(--muted)]">
        <div>{appointment.service_name} · {formatDateTime(appointment.start_at)}</div>
        {appointment.reason && <div className="text-[var(--text)]">{appointment.reason}</div>}
      </div>
    );
  } else if (message) {
    title = message.sender_type === "patient" ? "رسالة من العميل" : message.sender_type === "ai" ? "رد Linka" : "رد الفريق";
    body = (
      <div className="space-y-2">
        <p className="whitespace-pre-wrap text-sm leading-6">{message.content || "رسالة بدون نص"}</p>
        <div className="flex flex-wrap gap-2 text-xs text-[var(--muted)]">
          <span>{labelForChannel(message.channel)}</span>
          <span>·</span>
          <span>{labelForStatus(message.delivery_status)}</span>
          <Link href={`/inbox/${message.conversation_id}`} className="font-bold text-[var(--interactive)]">فتح المحادثة</Link>
        </div>
      </div>
    );
  } else if (task) {
    title = task.event_type === "completed" ? `تمت متابعة: ${task.title}` : `متابعة: ${task.title}`;
    body = (
      <div className="space-y-2 text-sm">
        <div className="text-[var(--muted)]">موعد المتابعة: {formatDateTime(task.due_at)}</div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge domain="task" status={task.status} showIcon={false} />
          <Link href="/tasks?scope=all" className="self-center text-xs font-bold text-[var(--interactive)]">فتح المتابعات</Link>
        </div>
      </div>
    );
  } else if (handoff) {
    title = handoffEventLabels[handoff.event_type] || "تحديث متابعة الفريق";
    body = (
      <div className="space-y-2 text-sm">
        <p className="leading-6">{handoff.reason}</p>
        <Link href={`/inbox/${handoff.conversation_id}`} className="text-xs font-bold text-[var(--interactive)]">فتح المحادثة</Link>
      </div>
    );
  } else if (payment) {
    title = payment.transaction_type === "refund" ? "تم تسجيل استرداد" : "تم تسجيل دفعة";
    body = <div className="space-y-2 text-sm"><div className="text-lg font-black">{payment.transaction_type === "refund" ? "−" : "+"}{formatMoney(payment.amount_minor,payment.currency)}</div><div className="text-[var(--muted)]">{({cash:"نقدي",card:"بطاقة",bank_transfer:"تحويل بنكي",wallet:"محفظة إلكترونية",online:"دفع إلكتروني",other:"أخرى"} as Record<string,string>)[payment.payment_method] || "طريقة دفع غير محددة"}</div>{payment.reason && <div>{payment.reason}</div>}{payment.appointment_id ? <Link href={`/appointments/${payment.appointment_id}`} className="text-xs font-bold text-[var(--interactive)]">فتح الموعد</Link> : <div className="text-xs text-[var(--muted)]">دفعة عامة مسجلة على حساب العميل</div>}</div>;
  }

  return (
    <div className="relative flex gap-4 pb-7 last:pb-0">
      {!isLast && <div className="absolute bottom-0 right-[17px] top-9 w-px bg-[var(--border)]" />}
      <div className="relative z-10 grid size-9 shrink-0 place-items-center rounded-full border border-[var(--border)] bg-white text-[var(--interactive)]">
        <TimelineIcon event={event} />
      </div>
      <div className="min-w-0 flex-1 pt-0.5">
        <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            <div className="font-bold">{title}</div>
            {note?.is_pinned && <span className="text-[11px] font-bold text-[var(--interactive)]">مثبتة</span>}
            {appointment && <StatusBadge domain="appointment" status={appointment.to_status || appointment.status} showIcon={false} />}
          </div>
          <div className="text-xs text-[var(--muted)]">{formatDateTime(event.occurred_at)}</div>
        </div>
        <div className="mt-1 text-xs text-[var(--muted)]">بواسطة {actorLabel(event)}</div>
        {body && <div className="mt-3 rounded-xl bg-[var(--surface-2)] p-3">{body}</div>}
        {event.kind === "appointment" && (
          <Link href={`/appointments?patient_id=${patientId}`} className="mt-2 inline-block text-xs font-bold text-[var(--interactive)]">عرض حجوزات العميل</Link>
        )}
      </div>
    </div>
  );
}

export default async function PatientProfilePage({ params }: { params: Promise<{ patientId: string }> }) {
  const { patientId } = await params;
  const profile = await tiaRequest<PatientProfile>(`/crm/patients/${patientId}/profile?timeline_limit=75`);
  const { patient, stats } = profile;
  const patientName = `${patient.first_name} ${patient.last_name || ""}`.trim();
  const nextAction = nextOperationalAction(stats);

  return (
    <>
      <PageHeader
        title={patientName}
        description={patient.phone || "ملف العميل"}
        action={
          <div className="flex flex-wrap gap-2">
            <Link href={`/appointments?patient_id=${patient.id}&book=1`} className={buttonVariants()}>
              <CalendarClock size={15} /> حجز موعد
            </Link>
            {profile.latest_conversation_id && (
              <Link href={`/inbox/${profile.latest_conversation_id}`} className={buttonVariants({ variant: "outline" })}>
                <MessageSquareMore size={15} /> المحادثة
              </Link>
            )}
            <Link href="/patients" className={buttonVariants({ variant: "ghost" })}>
              <ArrowLeft size={15} /> العملاء
            </Link>
          </div>
        }
      />

      <div className="mb-5 flex flex-wrap gap-2">
        <StatusBadge domain="patient" status={patient.status} showIcon={false} />
        <Badge>{labelForSource(patient.source)}</Badge>
        {profile.tags.map((tag) => <Badge key={tag.id} tone="purple"><Tag size={11} className="ml-1" />{tag.name}</Badge>)}
      </div>

      <section className="grid gap-3 rounded-2xl border border-[var(--border)] bg-white p-4 shadow-sm sm:grid-cols-3">
        <div className="rounded-xl bg-[var(--surface-2)] p-4">
          <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><CalendarClock size={15} /> الخطوة القادمة</div>
          <div className="mt-2 text-sm font-black">
            {nextAction
              ? `${nextAction.kind === "task" ? "متابعة" : "موعد"} · ${formatDateTime(nextAction.at)}`
              : "لا يوجد إجراء مجدول"}
          </div>
          {nextAction?.kind === "task" ? (
            <Link href={`/tasks?view=all&patient_id=${patient.id}`} className="mt-2 inline-flex text-xs font-bold text-[var(--interactive)]">فتح متابعة العميل</Link>
          ) : nextAction?.kind === "appointment" ? (
            <Link href={`/appointments?patient_id=${patient.id}`} className="mt-2 inline-flex text-xs font-bold text-[var(--interactive)]">عرض مواعيد العميل</Link>
          ) : (
            <span className="mt-2 block text-xs text-[var(--muted)]">أضف موعدًا أو متابعة عندما يكون هناك إجراء مطلوب.</span>
          )}
        </div>
        <div className={`rounded-xl border p-4 ${stats.overdue_tasks || stats.active_handoffs ? "border-amber-200 bg-amber-50/70" : "border-[var(--border)]"}`}>
          <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><CircleAlert size={15} /> يحتاج انتباه</div>
          <div className="mt-2 text-sm font-black">{stats.overdue_tasks || stats.active_handoffs ? `${stats.overdue_tasks + stats.active_handoffs} إجراء` : "لا شيء عاجل"}</div>
          <div className="mt-1 space-y-1 text-xs text-[var(--muted)]">
            {stats.overdue_tasks > 0 && <div>{stats.overdue_tasks} متابعة متأخرة</div>}
            {stats.active_handoffs > 0 && <div>{stats.active_handoffs} تصعيد نشط</div>}
            {!stats.overdue_tasks && !stats.active_handoffs && <div>لا توجد متابعة متأخرة أو تصعيد نشط</div>}
          </div>
          {stats.overdue_tasks > 0 && (
            <Link href={`/tasks?view=pending&patient_id=${patient.id}`} className="mt-2 inline-flex text-xs font-bold text-amber-800 underline underline-offset-2">فتح المتابعات المتأخرة</Link>
          )}
        </div>
        <div className="rounded-xl border border-[var(--border)] p-4">
          <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><MessageSquareMore size={15} /> آخر تواصل</div>
          <div className="mt-2 text-sm font-black">{patient.last_contact_at ? formatDateTime(patient.last_contact_at) : "لا يوجد تواصل مسجل"}</div>
          <div className="mt-1 text-xs text-[var(--muted)]">{stats.open_conversations ? `${stats.open_conversations} محادثة مفتوحة` : `${stats.total_conversations} محادثة في السجل`}</div>
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-2 border-t border-[var(--border)] pt-3 text-xs text-[var(--muted)] sm:col-span-3">
          <span>الحجوزات <b className="text-[var(--text)]">{stats.total_appointments}</b></span>
          <span>مكتملة <b className="text-[var(--text)]">{stats.completed_appointments}</b></span>
          <span>عدم حضور <b className="text-[var(--text)]">{stats.no_show_appointments}</b></span>
          <span>متابعات مفتوحة <b className="text-[var(--text)]">{stats.open_tasks}</b></span>
        </div>
      </section>

      <div className="mt-6 grid gap-6 xl:grid-cols-[1.45fr_.75fr]">
        <Card className="order-2 xl:order-1">
          <CardHeader className="flex-row items-center justify-between">
            <div>
              <CardTitle>سجل العميل</CardTitle>
              <p className="mt-1 text-xs text-[var(--muted)]">المواعيد والمحادثات والمتابعات والمدفوعات والملاحظات في ترتيب زمني واحد.</p>
            </div>
            <Clock3 size={18} className="text-[var(--muted)]" />
          </CardHeader>
          <CardContent>
            {profile.timeline.length ? (
              <div>{profile.timeline.map((event, index) => <TimelineEvent key={event.id} event={event} patientId={patient.id} isLast={index === profile.timeline.length - 1} />)}</div>
            ) : (
              <div className="py-12 text-center text-sm text-[var(--muted)]">لا يوجد نشاط مسجل حتى الآن.</div>
            )}
          </CardContent>
        </Card>

        <div className="order-1 space-y-5 xl:order-2">
          <Card>
            <CardHeader><CardTitle>بيانات العميل</CardTitle></CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div className="flex justify-between gap-4"><span className="text-[var(--muted)]">رقم الهاتف</span><b className="text-left" dir="ltr">{patient.phone || "—"}</b></div>
              <div className="flex justify-between gap-4"><span className="text-[var(--muted)]">مصدر العميل</span><b>{labelForSource(patient.source)}</b></div>
              <div className="flex justify-between gap-4"><span className="text-[var(--muted)]">آخر تواصل</span><b className="text-left">{formatDateTime(patient.last_contact_at)}</b></div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>إجراءات سريعة</CardTitle>
              <p className="text-xs leading-5 text-[var(--muted)]">أضف متابعة أو ملاحظة من ملف العميل مباشرة.</p>
            </CardHeader>
            <CardContent className="space-y-3">
              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="flex min-h-10 cursor-pointer items-center text-sm font-bold text-slate-800">جدولة متابعة</summary>
                <form action={createPatientTask} className="mt-4 space-y-3">
                  <input type="hidden" name="patient_id" value={patient.id} />
                  <input type="hidden" name="conversation_id" value={profile.latest_conversation_id || ""} />
                  <label className="block text-xs font-bold text-slate-700">
                    سبب المتابعة
                    <Input name="title" required maxLength={200} placeholder="مثال: متابعة نتيجة الاستشارة" className="mt-1" />
                  </label>
                  <label className="block text-xs font-bold text-slate-700">
                    موعد المتابعة
                    <Input name="due_at" type="datetime-local" required className="mt-1" />
                  </label>

                  <Button className="w-full"><ListTodo size={15} /> حفظ المتابعة</Button>
                </form>
              </details>

              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="flex min-h-10 cursor-pointer items-center text-sm font-bold text-slate-800">إضافة ملاحظة</summary>
                <form action={addPatientNote} className="mt-4 space-y-3">
                  <input type="hidden" name="patient_id" value={patient.id} />
                  <Textarea name="content" required placeholder="اكتب المعلومة المهمة للفريق..." />
                  <Button variant="secondary" className="w-full"><StickyNote size={15} /> حفظ الملاحظة</Button>
                </form>
              </details>
            </CardContent>
          </Card>

          <PatientPackagePanel patientId={patient.id} />
          <PatientPulsePanel patientId={patient.id} />

          <Card>
            <CardHeader><CardTitle>ملاحظات العميل</CardTitle></CardHeader>
            <CardContent className="space-y-3">
              {profile.notes.length ? profile.notes.slice(0, 5).map((note) => (
                <div key={note.id} className="rounded-xl border border-[var(--border)] p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex flex-wrap gap-1.5"><Badge>{noteLabels[note.note_type] || "ملاحظة"}</Badge>{note.is_pinned && <span className="rounded-full bg-[var(--interactive-soft)] px-2 py-1 text-[10px] font-bold text-[var(--interactive-strong)]">مثبتة</span>}</div>
                    <div className="text-[11px] text-[var(--muted)]">{formatDateTime(note.created_at)}</div>
                  </div>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{note.content}</p>
                </div>
              )) : <div className="text-sm text-[var(--muted)]">لا توجد ملاحظات مسجلة.</div>}
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
