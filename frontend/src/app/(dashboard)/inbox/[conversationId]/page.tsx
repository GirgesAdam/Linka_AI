import Link from "next/link";
import { ArrowRight, Bot, CheckCircle2, UserRound } from "lucide-react";
import { ConversationReadMarker } from "@/components/conversation-read-marker";
import { LiveRouteRefresh } from "@/components/live-route-refresh";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { formatDateTime } from "@/lib/format";
import { labelForChannel } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import { getAppContext } from "@/lib/tia/workspace";
import type { InboxConversation, WorkspaceMember } from "@/lib/types";
import {
  assignHandoff,
  claimHandoff,
  resolveHandoff,
  sendWhatsappFollowup,
  takeOverConversation,
} from "../actions";
import { ConversationScroll } from "./conversation-scroll";
import { InboxMessageBody } from "./media-message";
import { InboxReplyForm } from "./reply-form";

const WHATSAPP_FREEFORM_WINDOW_MS = 24 * 60 * 60 * 1000;

const categoryLabels: Record<string, string> = {
  customer_request: "طلب من العميل",
  booking: "حجز أو موعد",
  payment: "دفع أو استرداد",
  complaint: "شكوى",
  medical: "استفسار يحتاج مراجعة",
  other: "متابعة عامة",
};

function senderLabel(senderType: string) {
  if (senderType === "patient") return "العميل";
  if (senderType === "staff") return "الفريق";
  if (senderType === "ai") return "Linka";
  return "العيادة";
}

function contextString(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function freeformReplyAllowed(conversation: InboxConversation) {
  if (conversation.channel !== "whatsapp") return true;
  for (let index = conversation.messages.length - 1; index >= 0; index -= 1) {
    const message = conversation.messages[index];
    if (message.sender_type !== "patient" || message.direction !== "inbound") continue;
    const inboundAt = Date.parse(message.created_at);
    return Number.isFinite(inboundAt) && Date.now() < inboundAt + WHATSAPP_FREEFORM_WINDOW_MS;
  }
  return false;
}

export default async function ConversationPage({
  params,
  searchParams,
}: {
  params: Promise<{ conversationId: string }>;
  searchParams: Promise<{ followup?: string }>;
}) {
  const [{ conversationId }, query] = await Promise.all([params, searchParams]);
  const [conversation, ctx] = await Promise.all([
    tiaRequest<InboxConversation>(`/inbox/conversations/${conversationId}`),
    getAppContext(),
  ]);
  const members =
    ctx.workspace.role === "admin"
      ? await tiaRequest<WorkspaceMember[]>("/auth/workspace/members")
      : [];

  const handoff = conversation.active_handoff;
  const assignedToMe = handoff?.assigned_user_id === ctx.me.user.id;
  const unassigned = Boolean(handoff && !handoff.assigned_user_id);
  const assigneeName = conversation.assigned_user?.full_name || conversation.assigned_user?.email;
  const canResolve = Boolean(
    handoff && handoff.status !== "resolved" && (assignedToMe || ctx.workspace.role === "admin"),
  );
  const latestCustomerMessage = contextString(handoff?.context_json?.latest_customer_message);
  const patientName = `${conversation.patient.first_name} ${conversation.patient.last_name || ""}`.trim();
  const canSendFreeform = freeformReplyAllowed(conversation);
  const followupState = query.followup === "pending" || query.followup === "unavailable" ? query.followup : null;

  return (
    <>
      <LiveRouteRefresh />
      <ConversationReadMarker
        conversationId={conversation.id}
        unreadCount={conversation.unread_count}
      />
      <PageHeader
        title={patientName}
        description={`${labelForChannel(conversation.channel)} · ${conversation.patient.phone || "لا يوجد رقم هاتف مسجل"}`}
        action={
          <Link href="/inbox" className={buttonVariants({ variant: "outline" })}>
            <ArrowRight size={15} /> المحادثات
          </Link>
        }
      />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="flex h-[calc(100dvh-9rem)] min-h-[520px] max-h-[900px] flex-col overflow-hidden">
          <CardHeader className="shrink-0 border-b border-[var(--border)] py-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle>المحادثة</CardTitle>
              <div className="flex flex-wrap gap-2">
                <Badge tone={conversation.owner_type === "human" ? "yellow" : "green"}>
                  {conversation.owner_type === "human" ? "الفريق يتولى الرد" : "Linka تتولى الرد"}
                </Badge>
                <StatusBadge domain="conversation" status={conversation.status} />
              </div>
            </div>
          </CardHeader>

          <CardContent className="flex min-h-0 flex-1 flex-col p-0">
            <ConversationScroll messageCount={conversation.messages.length}>
              {conversation.messages.map((message) => {
                const incoming = message.sender_type === "patient";
                const isAi = message.sender_type === "ai";
                const deliveryFailed =
                  message.direction === "outbound" && message.delivery_status === "failed";
                return (
                  <div key={message.id} className={`flex ${incoming ? "justify-start" : "justify-end"}`}>
                    <div
                      className={`max-w-[88%] rounded-2xl px-4 py-3 sm:max-w-[76%] ${
                        incoming ? "bg-[var(--surface-2)]" : "bg-[var(--accent-soft)]"
                      } ${deliveryFailed ? "ring-1 ring-red-200" : ""}`}
                    >
                      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-bold text-[var(--muted)]">
                        {isAi ? <Bot size={12} /> : <UserRound size={12} />}
                        {senderLabel(message.sender_type)}
                      </div>
                      <InboxMessageBody message={message} />
                      {deliveryFailed && (
                        <div
                          role="alert"
                          className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold leading-5 text-red-800"
                        >
                          الرسالة لم تصل للعميل، ولا توجد محاولة تلقائية معلّقة. راجع حالة واتساب وبيانات العميل قبل إعادة الإرسال.
                        </div>
                      )}
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-[var(--muted)]">
                        <span>{formatDateTime(message.created_at)}</span>
                        {message.direction === "outbound" && (
                          <StatusBadge domain="message" status={message.delivery_status} showIcon={false} />
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
              {!conversation.messages.length && (
                <div className="py-16 text-center text-sm text-[var(--muted)]">لا توجد رسائل حتى الآن.</div>
              )}
            </ConversationScroll>

            <div className="shrink-0 border-t border-[var(--border)] bg-white p-4">
              {conversation.status === "closed" ? (
                <div className="rounded-xl bg-slate-50 p-4 text-sm text-slate-600">
                  هذه المحادثة مغلقة. يمكن متابعة العميل من ملفه عند الحاجة.
                </div>
              ) : conversation.owner_type === "ai" ? (
                <div className="flex flex-col gap-3 rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-4 text-sm text-slate-800 sm:flex-row sm:items-center sm:justify-between">
                  <span>Linka تتولى المحادثة حاليًا. استلم المحادثة إذا احتاج الفريق إلى الرد مباشرة.</span>
                  <form action={takeOverConversation}>
                    <input type="hidden" name="conversation_id" value={conversation.id} />
                    <Button size="sm">استلام المحادثة</Button>
                  </form>
                </div>
              ) : handoff && assignedToMe && conversation.channel === "whatsapp" && !canSendFreeform ? (
                <div className="space-y-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-950">
                  <div>
                    <div className="font-bold">انتهت نافذة واتساب للرد الحر بعد 24 ساعة.</div>
                    <p className="mt-1 leading-6 text-amber-900">
                      بدل إرسال رسالة حرة قد ترفضها Meta، استخدم رسالة متابعة معتمدة. بعد ما العميل يرد، تقدر تكمل المحادثة بشكل طبيعي.
                    </p>
                  </div>
                  {followupState === "pending" && (
                    <div className="rounded-lg border border-amber-200 bg-white/70 px-3 py-2 text-xs font-semibold leading-5">
                      Linka طلبت قالب المتابعة من Meta. الإرسال هيتاح بمجرد اعتماد القالب، من غير ما يعطل باقي واتساب أو الـAutomations.
                    </div>
                  )}
                  {followupState === "unavailable" && (
                    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs font-semibold leading-5 text-red-800">
                      رسالة المتابعة المعتمدة غير متاحة حاليًا. راجع ربط واتساب وموافقة العميل قبل المحاولة مرة أخرى.
                    </div>
                  )}
                  <form action={sendWhatsappFollowup}>
                    <input type="hidden" name="conversation_id" value={conversation.id} />
                    <Button size="sm">إرسال متابعة معتمدة</Button>
                  </form>
                </div>
              ) : handoff && assignedToMe ? (
                <InboxReplyForm conversationId={conversation.id} />
              ) : handoff && unassigned ? (
                <div className="flex flex-col gap-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-900 sm:flex-row sm:items-center sm:justify-between">
                  <span>هذه المحادثة تحتاج متابعة من أحد أعضاء الفريق.</span>
                  <form action={claimHandoff}>
                    <input type="hidden" name="handoff_id" value={handoff.id} />
                    <input type="hidden" name="conversation_id" value={conversation.id} />
                    <Button size="sm">استلام المتابعة</Button>
                  </form>
                </div>
              ) : handoff ? (
                <div className="rounded-xl bg-slate-50 p-4 text-sm text-slate-700">
                  يتابع هذه المحادثة <b>{assigneeName || "عضو آخر من الفريق"}</b>.
                </div>
              ) : (
                <div className="flex flex-col gap-3 rounded-xl bg-amber-50 p-4 text-sm text-amber-900 sm:flex-row sm:items-center sm:justify-between">
                  <span>المحادثة بانتظار متابعة الفريق.</span>
                  <form action={takeOverConversation}>
                    <input type="hidden" name="conversation_id" value={conversation.id} />
                    <Button size="sm">استلام المحادثة</Button>
                  </form>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>متابعة المحادثة</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-[var(--muted)]">المتابعة الحالية</span>
                  <b>{conversation.owner_type === "human" ? "الفريق" : "Linka"}</b>
                </div>
                {conversation.owner_type === "human" && (
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-[var(--muted)]">المسؤول</span>
                    <span className="text-left font-bold">{assigneeName || "لم يتم الإسناد بعد"}</span>
                  </div>
                )}
                {conversation.unread_count > 0 && (
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-[var(--muted)]">رسائل جديدة</span>
                    <Badge tone="blue">{conversation.unread_count}</Badge>
                  </div>
                )}
              </div>

              {handoff && (
                <div className="space-y-3 border-t border-[var(--border)] pt-4">
                  <div className="flex flex-wrap gap-2">
                    <StatusBadge domain="priority" status={handoff.priority} />
                    <StatusBadge domain="handoff" status={handoff.status} />
                  </div>
                  <div>
                    <div className="text-xs font-bold text-[var(--muted)]">سبب المتابعة</div>
                    <p className="mt-1 leading-6">{handoff.reason}</p>
                  </div>
                  {handoff.category && (
                    <div className="text-xs text-[var(--muted)]">
                      {categoryLabels[handoff.category] || "متابعة من الفريق"}
                    </div>
                  )}
                  {latestCustomerMessage && latestCustomerMessage !== handoff.reason && (
                    <div className="rounded-xl bg-[var(--surface-2)] p-3">
                      <div className="text-xs font-bold text-[var(--muted)]">آخر رسالة مهمة</div>
                      <p className="mt-1 leading-6">{latestCustomerMessage}</p>
                    </div>
                  )}

                  {ctx.workspace.role === "admin" && handoff.status !== "resolved" && (
                    <form action={assignHandoff} className="space-y-2 border-t border-[var(--border)] pt-3">
                      <input type="hidden" name="handoff_id" value={handoff.id} />
                      <input type="hidden" name="conversation_id" value={conversation.id} />
                      <label className="block text-xs font-bold text-[var(--muted)]">إسناد المتابعة</label>
                      <select
                        name="user_id"
                        defaultValue={handoff.assigned_user_id || ""}
                        required
                        className="form-control h-10 min-h-10"
                      >
                        <option value="" disabled>اختر عضوًا من الفريق</option>
                        {members
                          .filter((member) => member.is_active)
                          .map((member) => (
                            <option key={member.user_id} value={member.user_id}>
                              {member.full_name || member.email}
                            </option>
                          ))}
                      </select>
                      <Button variant="outline" className="w-full">حفظ المسؤول</Button>
                    </form>
                  )}

                  {canResolve && (
                    <form action={resolveHandoff} className="space-y-3 border-t border-[var(--border)] pt-3">
                      <input type="hidden" name="handoff_id" value={handoff.id} />
                      <input type="hidden" name="conversation_id" value={conversation.id} />
                      <Textarea name="resolution_note" placeholder="ملاحظة ختامية - اختياري" />
                      <label className="flex items-start gap-2 text-xs leading-5 text-[var(--muted)]">
                        <input type="checkbox" name="close_conversation" className="mt-1" />
                        إغلاق المحادثة وعدم إعادتها لـ Linka
                      </label>
                      <Button variant="secondary" className="w-full">
                        <CheckCircle2 size={16} />
                        سلّم المحادثة لـ Linka
                      </Button>
                    </form>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>العميل</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <span className="text-[var(--muted)]">رقم الهاتف</span>
                <b dir="ltr">{conversation.patient.phone || "—"}</b>
              </div>
              <Link href={`/patients/${conversation.patient.id}`} className={buttonVariants({ variant: "outline", size: "sm" })}>
                فتح ملف العميل
              </Link>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}
