import Link from "next/link";
import { AlertTriangle, Bot, MessageSquareMore, Search, UserRound } from "lucide-react";

import { EmptyState } from "@/components/empty-state";
import { LiveRouteRefresh } from "@/components/live-route-refresh";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Card, CardContent } from "@/components/ui/card";
import { FilterChip } from "@/components/ui/filter-chip";
import { formatDateTime } from "@/lib/format";
import { labelForChannel } from "@/lib/status";
import { tiaRequest } from "@/lib/tia/api";
import type { InboxConversationListItem } from "@/lib/types";

type InboxSearchParams = { owner?: string; status?: string; mine?: string; unread?: string; q?: string; page?: string };
type InboxFilters = Omit<InboxSearchParams, "page">;

type InboxChannelConnection = {
  channel: string;
  provider: string;
  status: string;
  updated_at: string;
  config_json?: Record<string, unknown>;
};

type DeliveryHealthNotice = {
  tone: "amber" | "red";
  title: string;
  message: string;
};

const PAGE_SIZE = 50;
const ownerOptions = [["", "الكل"], ["human", "الفريق"], ["ai", "Linka"]] as const;
const statusOptions = [["", "كل الحالات"], ["open", "مفتوحة"], ["pending", "بانتظار رد"], ["closed", "مغلقة"]] as const;

function filterHref(current: InboxFilters, key: keyof InboxFilters, value: string) {
  const params = new URLSearchParams();
  for (const [currentKey, currentValue] of Object.entries(current)) {
    if (currentValue && currentKey !== key) params.set(currentKey, currentValue);
  }
  if (value) params.set(key, value);
  const query = params.toString();
  return query ? `/inbox?${query}` : "/inbox";
}

function pageHref(filters: InboxFilters, page: number) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  if (page > 1) params.set("page", String(page));
  const query = params.toString();
  return query ? `/inbox?${query}` : "/inbox";
}

function attentionLabel(conversation: InboxConversationListItem) {
  if (conversation.last_message?.direction === "outbound" && conversation.last_message.delivery_status === "failed") {
    return { label: "فشل إرسال", className: "text-red-700" };
  }
  if (conversation.active_handoff && !conversation.active_handoff.assigned_user_id) {
    return { label: "تحتاج استلام", className: "text-amber-700" };
  }
  if (conversation.active_handoff) {
    return { label: "تدخل بشري", className: "text-amber-700" };
  }
  if (conversation.unread_count > 0) {
    return { label: "رسائل جديدة", className: "text-[var(--accent-strong)]" };
  }
  return null;
}

function senderLabel(senderType?: string) {
  if (senderType === "patient") return "العميل";
  if (senderType === "staff") return "الفريق";
  if (senderType === "ai") return "Linka";
  return "رسالة";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function deliveryHealthNotice(connections: InboxChannelConnection[]): DeliveryHealthNotice | null {
  const connection = [...connections]
    .filter(
      (item) =>
        item.channel === "whatsapp" &&
        item.provider === "meta_cloud" &&
        item.status !== "disconnected",
    )
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0];

  if (!connection) return null;

  const providerHealth = asRecord(connection.config_json?.provider_health);
  if (!providerHealth) return null;

  const state = String(providerHealth.state || "");
  const errorCode = String(providerHealth.current_error_code || "");
  const actionRequired = String(providerHealth.action_required || "");

  if (actionRequired === "reconnect_meta" || errorCode === "190") {
    return {
      tone: "red",
      title: "إرسال واتساب يحتاج تدخل الإدارة",
      message: "اتصال Meta يحتاج إعادة ربط. الرسائل الجديدة قد تفشل لحد ما مدير العيادة يعيد ربط الحساب.",
    };
  }

  if (
    actionRequired === "meta_account_review" ||
    state === "disabled" ||
    errorCode === "131031"
  ) {
    return {
      tone: "red",
      title: "Meta أوقفت أو قيّدت حساب واتساب",
      message: "مدير العيادة يحتاج يراجع حالة الحساب في Meta قبل ما إرسال واتساب يرجع يشتغل بصورة طبيعية.",
    };
  }

  if (state === "degraded") {
    return {
      tone: "amber",
      title: "اتصال واتساب غير مستقر مؤقتًا",
      message: "قد تتأخر أو تفشل بعض الرسائل الجديدة أثناء مشكلة الاتصال الحالية مع Meta.",
    };
  }

  return null;
}

export default async function InboxPage({ searchParams }: { searchParams: Promise<InboxSearchParams> }) {
  const raw = await searchParams;
  const filters: InboxFilters = {
    owner: raw.owner === "ai" || raw.owner === "human" ? raw.owner : "",
    status: ["open", "pending", "closed"].includes(raw.status || "") ? raw.status : "",
    mine: raw.mine === "1" ? "1" : "",
    unread: raw.unread === "1" ? "1" : "",
    q: (raw.q || "").trim().slice(0, 120),
  };
  const parsedPage = Number.parseInt(raw.page || "1", 10);
  const page = Number.isSafeInteger(parsedPage) && parsedPage > 0 ? parsedPage : 1;

  const query = new URLSearchParams({
    limit: String(PAGE_SIZE + 1),
    offset: String((page - 1) * PAGE_SIZE),
  });
  if (filters.owner) query.set("owner_type", filters.owner);
  if (filters.status) query.set("status", filters.status);
  if (filters.mine) query.set("assigned_to_me", "true");
  if (filters.unread) query.set("unread_only", "true");
  if (filters.q) query.set("q", filters.q);

  const [conversationPage, channelConnections, revision] = await Promise.all([
    tiaRequest<InboxConversationListItem[]>(`/inbox/conversations?${query.toString()}`),
    tiaRequest<InboxChannelConnection[]>("/channels/connections").catch(() => []),
    tiaRequest<{ revision: string }>("/inbox/revision"),
  ]);
  const hasNextPage = conversationPage.length > PAGE_SIZE;
  const conversations = conversationPage.slice(0, PAGE_SIZE);
  const deliveryHealth = deliveryHealthNotice(channelConnections);

  return (
    <>
      <LiveRouteRefresh initialRevision={revision.revision} watchUrl="/api/inbox/revision" intervalMs={5000} />
      <PageHeader
        title="الرسائل"
        description="كل محادثات العملاء في مكان واحد، مع توضيح المحادثات التي تديرها Linka والمحادثات التي تحتاج تدخل الفريق."
      />

      {deliveryHealth && (
        <div
          role="status"
          className={`mb-4 flex gap-3 rounded-2xl border p-4 text-sm ${
            deliveryHealth.tone === "red"
              ? "border-rose-200 bg-rose-50 text-rose-950"
              : "border-amber-200 bg-amber-50 text-amber-950"
          }`}
        >
          <AlertTriangle className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
          <div>
            <div className="font-black">{deliveryHealth.title}</div>
            <p className="mt-1 leading-6">{deliveryHealth.message}</p>
          </div>
        </div>
      )}

      <form action="/inbox" method="get" className="mb-4 flex flex-col gap-2 sm:flex-row">
        {filters.owner && <input type="hidden" name="owner" value={filters.owner} />}
        {filters.status && <input type="hidden" name="status" value={filters.status} />}
        {filters.mine && <input type="hidden" name="mine" value={filters.mine} />}
        {filters.unread && <input type="hidden" name="unread" value={filters.unread} />}
        <div className="relative min-w-0 flex-1">
          <Search
            size={17}
            aria-hidden="true"
            className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400"
          />
          <input
            type="search"
            name="q"
            defaultValue={filters.q}
            maxLength={120}
            className="form-control w-full pr-10"
            placeholder="ابحث باسم العميل أو رقم الموبايل"
            aria-label="البحث في المحادثات باسم العميل أو رقم الموبايل"
          />
        </div>
        <button
          type="submit"
          className="inline-flex h-10 items-center justify-center rounded-xl bg-[var(--accent)] px-5 text-sm font-bold text-white transition hover:bg-[var(--accent-strong)]"
        >
          بحث
        </button>
        {filters.q && (
          <Link
            href={filterHref(filters, "q", "")}
            className="inline-flex h-10 items-center justify-center rounded-xl border border-[var(--border)] px-4 text-sm font-bold text-slate-700 transition hover:bg-slate-50"
          >
            مسح البحث
          </Link>
        )}
      </form>

      <div className="surface-toolbar mb-4">
        {ownerOptions.map(([value, label]) => (
          <FilterChip key={value || "all"} href={filterHref(filters, "owner", value)} active={filters.owner === value}>
            {label}
          </FilterChip>
        ))}
        <span className="mx-1 hidden h-8 w-px bg-slate-200 sm:block" />
        {statusOptions.map(([value, label]) => (
          <FilterChip key={value || "all"} href={filterHref(filters, "status", value)} active={filters.status === value}>
            {label}
          </FilterChip>
        ))}
        <FilterChip href={filterHref(filters, "mine", filters.mine ? "" : "1")} active={Boolean(filters.mine)}>
          مسندة لي
        </FilterChip>
        <FilterChip href={filterHref(filters, "unread", filters.unread ? "" : "1")} active={Boolean(filters.unread)}>
          غير مقروءة
        </FilterChip>
      </div>

      <Card>
        <CardContent className="p-0">
          {conversations.length ? (
            <div className="divide-y divide-[var(--border)]">
              {conversations.map((conversation) => {
                const patientName = `${conversation.patient.first_name} ${conversation.patient.last_name || ""}`.trim();
                const preview = conversation.last_message?.content?.trim() || (conversation.last_message ? "رسالة غير نصية" : "لا توجد رسائل بعد");
                const assignee = conversation.assigned_user?.full_name || conversation.assigned_user?.email;
                const attention = attentionLabel(conversation);
                return (
                  <Link
                    href={`/inbox/${conversation.id}`}
                    key={conversation.id}
                    className="grid gap-3 p-4 transition hover:bg-slate-50 sm:p-5 md:grid-cols-[minmax(190px,.8fr)_minmax(260px,1.5fr)_auto] md:items-center"
                  >
                    <div className="min-w-0">
                      {attention && (
                        <div className={`mb-1 text-[11px] font-black ${attention.className}`}>{attention.label}</div>
                      )}
                      <div className="flex items-center gap-2">
                        <div className="truncate font-bold text-slate-900">{patientName}</div>
                        {conversation.unread_count > 0 && (
                          <span className="grid min-w-6 place-items-center rounded-full bg-[var(--accent)] px-1.5 py-0.5 text-[10px] font-black text-white">
                            {conversation.unread_count}
                          </span>
                        )}
                      </div>
                      <div className="mt-1 truncate text-xs text-[var(--muted)]">
                        {conversation.patient.phone || "بدون رقم هاتف"} · {labelForChannel(conversation.channel)}
                      </div>
                    </div>

                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge tone={conversation.owner_type === "human" ? "yellow" : "green"}>
                          <span className="inline-flex items-center gap-1">
                            {conversation.owner_type === "human" ? <UserRound size={11} /> : <Bot size={11} />}
                            {conversation.owner_type === "human" ? "مع الفريق" : "تديرها Linka"}
                          </span>
                        </Badge>
                        <StatusBadge domain="conversation" status={conversation.status} showIcon={false} />
                        {conversation.active_handoff && (
                          <StatusBadge domain="priority" status={conversation.active_handoff.priority} />
                        )}
                        {assignee && <Badge>{assignee}</Badge>}
                      </div>
                      <p className="mt-2 truncate text-sm text-[var(--muted)]">
                        <span className="font-semibold text-[var(--text)]">{senderLabel(conversation.last_message?.sender_type)}:</span> {preview}
                      </p>
                    </div>

                    <div className="whitespace-nowrap text-[11px] font-semibold text-[var(--muted)] md:text-xs">
                      {formatDateTime(conversation.last_message_at || conversation.started_at)}
                    </div>
                  </Link>
                );
              })}
            </div>
          ) : (
            <EmptyState
              icon={MessageSquareMore}
              title="لا توجد محادثات مطابقة"
              description={filters.q ? "جرّب اسمًا أو رقم موبايل مختلفًا، أو امسح البحث لعرض كل المحادثات." : "غيّر الفلاتر أو افتح كل المحادثات لعرض نتائج أخرى."}
            />
          )}
        </CardContent>
      </Card>

      {(page > 1 || hasNextPage) && (
        <nav className="mt-4 flex items-center justify-between gap-3" aria-label="صفحات المحادثات">
          {page > 1 ? (
            <Link
              href={pageHref(filters, page - 1)}
              className="inline-flex h-10 items-center justify-center rounded-xl border border-[var(--border)] px-4 text-sm font-bold text-slate-700 transition hover:bg-slate-50"
            >
              السابق
            </Link>
          ) : (
            <span />
          )}
          <span className="text-xs font-semibold text-[var(--muted)]">صفحة {page}</span>
          {hasNextPage ? (
            <Link
              href={pageHref(filters, page + 1)}
              className="inline-flex h-10 items-center justify-center rounded-xl border border-[var(--border)] px-4 text-sm font-bold text-slate-700 transition hover:bg-slate-50"
            >
              التالي
            </Link>
          ) : (
            <span />
          )}
        </nav>
      )}
    </>
  );
}
