import { PermissionLink as Link } from "@/components/permission-link";
import { CalendarClock, Megaphone, UsersRound } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";
import type { CRMCampaign, CRMCohort, ChannelConnection } from "@/lib/types";
import { SavedCohortFollowUpForm } from "./followup-form";
import { SavedCohortCampaignForm } from "./campaign-form";
import { getAppContext } from "@/lib/tia/workspace";

function metricText(metric: Record<string, unknown>) {
  const value = metric.value;
  const currency = typeof metric.currency === "string" ? metric.currency : null;
  if (currency && typeof value === "number") return formatMoney(value, currency);
  if (typeof value === "number") return value.toLocaleString("ar-EG");
  return typeof value === "string" ? value : "—";
}

function campaignStatus(status: string) {
  if (status === "confirmed") return "تم الإرسال";
  if (status === "cancelled") return "ملغاة";
  return "قيد التجهيز";
}

export default async function CohortPage({ params }: { params: Promise<{ cohortId: string }> }) {
  const { cohortId } = await params;
  const [cohort, channels, campaigns, ctx] = await Promise.all([
    tiaRequest<CRMCohort>(`/crm/cohorts/${cohortId}`),
    tiaRequest<ChannelConnection[]>("/channels/connections"),
    tiaRequest<CRMCampaign[]>(`/crm/cohorts/${cohortId}/campaigns`),
    getAppContext(),
  ]);
  const whatsappConnections = channels.filter((channel) => channel.channel === "whatsapp" && channel.status === "active");

  return <>
    <PageHeader
      title={cohort.name}
      description={`${cohort.member_count.toLocaleString("ar-EG")} عميل · ${cohort.period_label} · قائمة محفوظة من نتائج التقارير`}
    />
    <div className="mb-5"><Link href="/analytics" className="text-sm font-bold text-teal-800 hover:underline">← رجوع للتقارير</Link></div>

    <Card className="mb-5">
      <CardContent className="p-5">
        <div className="grid gap-4 md:grid-cols-3">
          <div className="rounded-2xl bg-slate-50 p-4">
            <div className="text-xs text-[var(--muted)]">عدد العملاء</div>
            <div className="mt-1 text-2xl font-black">{cohort.member_count.toLocaleString("ar-EG")}</div>
          </div>
          <div className="rounded-2xl bg-slate-50 p-4">
            <div className="text-xs text-[var(--muted)]">الفترة</div>
            <div className="mt-1 font-black">{cohort.period_label}</div>
          </div>
          <div className="rounded-2xl bg-slate-50 p-4">
            <div className="text-xs text-[var(--muted)]">تاريخ الحفظ</div>
            <div className="mt-1 font-black">{new Date(cohort.created_at).toLocaleDateString("ar-EG", { dateStyle: "medium" })}</div>
          </div>
        </div>
        <details className="mt-4 rounded-xl border border-[var(--border)] p-3 text-sm">
          <summary className="cursor-pointer font-black">كيف تم اختيار هذه القائمة؟</summary>
          <p className="mt-2 leading-6 text-[var(--muted)]">{cohort.question}</p>
          <p className="mt-2 text-xs text-[var(--muted)]">القائمة ثابتة كما كانت وقت حفظها، لذلك لن تتغير أسماؤها تلقائيًا مع تغير البيانات لاحقًا.</p>
        </details>
      </CardContent>
    </Card>

    <Card className="mb-5">
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div><CardTitle>العملاء</CardTitle><p className="mt-1 text-xs text-[var(--muted)]">افتح أي عميل لمراجعة ملفه أو التواصل معه بشكل فردي.</p></div>
        <span className="grid size-9 place-items-center rounded-xl bg-slate-100 text-slate-600"><UsersRound size={17}/></span>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {cohort.members.map(member => <div key={member.patient_id} className="rounded-2xl border border-[var(--border)] p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <Link href={`/patients/${member.patient_id}`} className="font-black text-teal-800 hover:underline">{member.patient_name}</Link>
                {member.patient_phone && <div className="mt-0.5 text-xs text-[var(--muted)]">{member.patient_phone}</div>}
              </div>
              <div className="flex flex-wrap gap-2">
                {member.snapshot_metrics.slice(0, 4).map((metric, index) => <span key={`${String(metric.key)}-${index}`} className="rounded-full bg-slate-100 px-2.5 py-1 text-xs"><b>{String(metric.label || "المؤشر")}:</b> {metricText(metric)}</span>)}
              </div>
            </div>
          </div>)}
          {!cohort.members.length && <div className="py-8 text-center text-sm text-[var(--muted)]">لا يوجد عملاء في هذه القائمة.</div>}
        </div>
      </CardContent>
    </Card>

    <Card className="mb-5">
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div><CardTitle>حملة واتساب</CardTitle><p className="mt-1 text-xs text-[var(--muted)]">راجع المستلمين والرسالة قبل أي إرسال.</p></div>
        <span className="grid size-9 place-items-center rounded-xl bg-slate-100 text-slate-600"><Megaphone size={17}/></span>
      </CardHeader>
      <CardContent><SavedCohortCampaignForm cohortId={cohort.id} cohortName={cohort.name} memberCount={cohort.member_count} whatsappConnections={whatsappConnections} isAdmin={ctx.workspace.role === "admin"}/></CardContent>
    </Card>

    {campaigns.length > 0 && <Card className="mb-5">
      <CardHeader><CardTitle>الحملات السابقة</CardTitle></CardHeader>
      <CardContent className="space-y-2">
        {campaigns.map((campaign) => <div key={campaign.id} className="rounded-xl border border-[var(--border)] p-3 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><b>{campaign.name}</b><div className="mt-1 text-xs text-[var(--muted)]">{campaignStatus(campaign.status)} · {campaign.eligible_count.toLocaleString("ar-EG")} مستلم جاهز وقت المراجعة</div></div>
            {campaign.status !== "draft" && <Link href={`/analytics/campaigns/${campaign.id}`} className="text-xs font-bold text-teal-800 hover:underline">عرض الأداء</Link>}
          </div>
        </div>)}
      </CardContent>
    </Card>}

    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div><CardTitle>متابعة الفريق</CardTitle><p className="mt-1 text-xs text-[var(--muted)]">أنشئ مهمة متابعة لكل عميل في القائمة بدون إرسال رسائل تلقائية.</p></div>
        <span className="grid size-9 place-items-center rounded-xl bg-slate-100 text-slate-600"><CalendarClock size={17}/></span>
      </CardHeader>
      <CardContent><SavedCohortFollowUpForm cohortId={cohort.id} cohortName={cohort.name} memberCount={cohort.member_count}/></CardContent>
    </Card>
  </>;
}
