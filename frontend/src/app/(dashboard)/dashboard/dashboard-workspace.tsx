import Link from "next/link";
import {
  ArrowUpLeft,
  CheckCircle2,
  CircleAlert,
  ListTodo,
  MessageSquareMore,
  WalletCards,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatMoney } from "@/lib/format";
import type {
  CRMTask,
  DashboardToday,
  DashboardTodayRevenue,
  InboxConversationListItem,
} from "@/lib/types";
import { setTaskStatus } from "../tasks/actions";
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

function formatMessageTime(value: string | null, timezone: string) {
  if (!value) return "بدون وقت مسجل";
  return new Intl.DateTimeFormat("ar-EG", {
    day: "numeric",
    month: "short",
    hour: "numeric",
    minute: "2-digit",
    timeZone: timezone,
  }).format(new Date(value));
}

function patientName(conversation: InboxConversationListItem) {
  return [conversation.patient.first_name, conversation.patient.last_name].filter(Boolean).join(" ");
}

function TeamMessageRow({
  conversation,
  timezone,
}: {
  conversation: InboxConversationListItem;
  timezone: string;
}) {
  const lastMessage = conversation.last_message?.content?.trim() || "محادثة ما زالت على الفريق";
  const priority = conversation.active_handoff?.priority;

  return (
    <Link
      href={`/inbox/${conversation.id}`}
      className="group grid min-h-16 gap-3 px-1 py-4 transition hover:bg-slate-50 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-3"
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-[15px] font-semibold text-slate-950">{patientName(conversation)}</span>
          {conversation.unread_count > 0 && (
            <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-bold text-violet-800">
              {conversation.unread_count} غير مقروءة
            </span>
          )}
          {priority && <StatusBadge domain="priority" status={priority} showIcon={false} />}
        </div>
        <p className="mt-1 line-clamp-2 break-words text-[13px] leading-5 text-slate-500">{lastMessage}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span>{formatMessageTime(conversation.last_message_at, timezone)}</span>
          <span aria-hidden="true">·</span>
          <span>{conversation.assigned_user?.full_name || conversation.assigned_user?.email || "على الفريق"}</span>
        </div>
      </div>
      <span className="inline-flex min-h-11 items-center gap-1 text-xs font-bold text-violet-700">
        فتح الشات <ArrowUpLeft size={14} />
      </span>
    </Link>
  );
}

function FollowUpRow({
  task,
  timezone,
  currentUserId,
  isAdmin,
}: {
  task: CRMTask;
  timezone: string;
  currentUserId: string;
  isAdmin: boolean;
}) {
  const canComplete = isAdmin || (
    task.execution_mode === "human" && task.assigned_user_id === currentUserId
  );

  return (
    <div className="grid min-h-16 gap-3 px-1 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-0">
            <div className="truncate text-[15px] font-semibold text-slate-950">{task.patient_name}</div>
            {task.patient_phone && (
              <div dir="ltr" className="mt-0.5 w-fit text-xs font-medium text-slate-500">{task.patient_phone}</div>
            )}
          </div>
          <StatusBadge domain="priority" status={task.priority} showIcon={false} />
          {task.is_overdue && (
            <span className="rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-bold text-red-700">متأخرة</span>
          )}
        </div>
        <p className="mt-1 break-words text-[13px] leading-5 text-slate-500">{task.title}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <span>{formatTaskTime(task.due_at, timezone)}</span>
          <span aria-hidden="true">·</span>
          <span>{task.status === "in_progress" ? "قيد التنفيذ" : "معلقة"}</span>
          {task.assigned_user_name && (
            <>
              <span aria-hidden="true">·</span>
              <span>{task.assigned_user_name}</span>
            </>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 sm:justify-end">
        {task.conversation_id && (
          <Link href={`/inbox/${task.conversation_id}`} className="inline-flex min-h-9 items-center gap-1 px-2 text-xs font-bold text-violet-700">
            <MessageSquareMore size={14} /> المحادثة
          </Link>
        )}
        <form action={setTaskStatus}>
          <input type="hidden" name="task_id" value={task.id} />
          <input type="hidden" name="patient_id" value={task.patient_id} />
          <input type="hidden" name="status" value="completed" />
          <Button
            type="submit"
            size="sm"
            variant="outline"
            disabled={!canComplete}
            title={canComplete ? "تعليم المتابعة كمكتملة" : "المتابعة على Linka أو عضو آخر من الفريق"}
          >
            <CheckCircle2 size={14} /> تم
          </Button>
        </form>
      </div>
    </div>
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
                <p className="mt-1 text-xs leading-5">المواعيد والرسائل والمتابعات ما زالت متاحة بشكل طبيعي.</p>
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
  teamMessages,
  followUps,
  revenue,
  currentUserId,
  isAdmin,
  teamMessagesUnavailable = false,
  followUpsUnavailable = false,
  revenueUnavailable = false,
}: {
  today: DashboardToday;
  teamMessages: InboxConversationListItem[];
  followUps: CRMTask[];
  revenue: DashboardTodayRevenue | null;
  currentUserId: string;
  isAdmin: boolean;
  teamMessagesUnavailable?: boolean;
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

      <section aria-labelledby="team-messages-heading" className="mt-8 sm:mt-10">
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 id="team-messages-heading" className="text-xl font-semibold text-slate-950">رسائل على الفريق</h2>
            <p className="mt-1 text-sm text-slate-500">كل محادثة ما زالت محتاجة الفريق، حتى لو بدأت من أيام سابقة</p>
          </div>
          <Link href="/inbox?owner=human" className="hidden text-xs font-bold text-violet-700 sm:inline-flex">كل الرسائل</Link>
        </div>

        <div className="rounded-2xl bg-white px-3 sm:px-4">
          {teamMessagesUnavailable ? (
            <div className="flex items-start gap-3 py-5 text-sm text-amber-950" role="status">
              <CircleAlert size={18} className="mt-0.5 shrink-0" />
              <div>
                <div className="font-semibold">تعذر تحميل رسائل الفريق مؤقتًا</div>
                <p className="mt-1 text-xs text-slate-500">تقدر تفتح الرسائل من القائمة الجانبية.</p>
              </div>
            </div>
          ) : teamMessages.length ? (
            <div className="divide-y divide-slate-100">
              {teamMessages.map((conversation) => (
                <TeamMessageRow key={conversation.id} conversation={conversation} timezone={today.timezone} />
              ))}
            </div>
          ) : (
            <div className="py-8 text-center">
              <MessageSquareMore size={20} className="mx-auto text-slate-300" />
              <div className="mt-2 text-sm font-semibold text-slate-800">مفيش رسائل معلقة على الفريق</div>
            </div>
          )}
        </div>
      </section>

      <section aria-labelledby="followups-heading" className="mt-8 sm:mt-10">
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h2 id="followups-heading" className="text-xl font-semibold text-slate-950">المتابعات المستحقة</h2>
            <p className="mt-1 text-sm text-slate-500">متابعات اليوم وأي متابعة متأخرة من الأيام السابقة لحد ما تتعمل تم</p>
          </div>
          <Link href="/tasks?scope=all" className="hidden text-xs font-bold text-violet-700 sm:inline-flex">كل المتابعات</Link>
        </div>

        <div className="rounded-2xl bg-white px-3 sm:px-4">
          {followUpsUnavailable ? (
            <div className="flex items-start gap-3 py-5 text-sm text-amber-950" role="status">
              <CircleAlert size={18} className="mt-0.5 shrink-0" />
              <div>
                <div className="font-semibold">تعذر تحميل المتابعات مؤقتًا</div>
                <p className="mt-1 text-xs text-slate-500">تقدر تكمل شغلك من المواعيد أو تفتح صفحة المتابعات.</p>
              </div>
            </div>
          ) : followUps.length ? (
            <div className="divide-y divide-slate-100">
              {followUps.map((task) => (
                <FollowUpRow
                  key={task.id}
                  task={task}
                  timezone={today.timezone}
                  currentUserId={currentUserId}
                  isAdmin={isAdmin}
                />
              ))}
            </div>
          ) : (
            <div className="py-8 text-center">
              <ListTodo size={20} className="mx-auto text-slate-300" />
              <div className="mt-2 text-sm font-semibold text-slate-800">مفيش متابعات مستحقة أو متأخرة</div>
            </div>
          )}
        </div>
      </section>

      <RevenueSection revenue={revenue} unavailable={revenueUnavailable} />
    </main>
  );
}
