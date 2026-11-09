import { requirePageAccess } from "@/lib/tia/require-page-access";
import { PermissionLink as Link } from "@/components/permission-link";
import { ArrowLeft, BarChart3, CheckCheck, Eye, Megaphone, Send, WalletCards } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";
import type { CampaignAnalyticsMetrics, CampaignAnalyticsOverview } from "@/lib/types";

const periods = [
  { key: "30", label: "30 يوم" },
  { key: "90", label: "90 يوم" },
  { key: "365", label: "سنة" },
  { key: "all", label: "كل التاريخ" },
] as const;

function MetricCard({ label, value, hint, icon }: { label:string; value:string; hint?:string; icon:React.ReactNode }) {
  return <div className="rounded-2xl border border-[var(--border)] bg-white p-4">
    <div className="flex items-center justify-between gap-3 text-xs font-bold text-[var(--muted)]"><span>{label}</span><span>{icon}</span></div>
    <div className="mt-2 text-2xl font-black">{value}</div>
    {hint && <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">{hint}</div>}
  </div>;
}

function metricsCards(metrics: CampaignAnalyticsMetrics) {
  return [
    { label:"تم الإرسال", value:metrics.sent_count.toLocaleString("ar-EG"), hint:`من ${metrics.recipient_count.toLocaleString("ar-EG")} مستلم`, icon:<Send size={16}/> },
    { label:"وصلت", value:`${metrics.delivery_rate.toLocaleString("ar-EG")}%`, hint:`${metrics.delivered_count.toLocaleString("ar-EG")} رسالة`, icon:<CheckCheck size={16}/> },
    { label:"اتقرت", value:`${metrics.read_rate.toLocaleString("ar-EG")}%`, hint:`${metrics.read_count.toLocaleString("ar-EG")} رسالة`, icon:<Eye size={16}/> },
    { label:"حجوزات متتبعة", value:metrics.tracked_booking_count.toLocaleString("ar-EG"), hint:`${metrics.booking_conversion_rate.toLocaleString("ar-EG")}% من الرسائل المرسلة`, icon:<BarChart3 size={16}/> },
    { label:"إيراد منسوب", value:formatMoney(metrics.attributed_revenue_minor, metrics.currency), hint:"مدفوعات فعلية موزعة على الحجوزات المتتبعة", icon:<WalletCards size={16}/> },
  ];
}

export default async function CampaignAnalyticsPage({ searchParams }: { searchParams: Promise<{ period?:string }> }) {
  await requirePageAccess("analytics.campaigns");
  const params = await searchParams;
  const period = periods.some((item) => item.key === params.period) ? params.period! : "90";
  const endpoint = period === "all" ? "/analytics/campaigns?all_history=true" : `/analytics/campaigns?days=${period}`;
  const data = await tiaRequest<CampaignAnalyticsOverview>(endpoint);

  return <>
    <PageHeader
      title="أداء الحملات"
      description="تابع الوصول والقراءة والحجوزات والإيراد المرتبط بالحملات في مكان واحد."
      action={<Link href="/analytics" className="inline-flex items-center gap-2 rounded-xl border border-[var(--border)] bg-white px-3 py-2 text-sm font-bold"><ArrowLeft size={16}/>رجوع للتقارير</Link>}
    />

    <div className="mb-5 flex flex-wrap gap-2">
      {periods.map((item) => <Link key={item.key} href={`/analytics/campaigns?period=${item.key}`} className={`rounded-full px-3 py-1.5 text-xs font-bold ${period === item.key ? "bg-slate-900 text-white" : "border border-[var(--border)] bg-white"}`}>{item.label}</Link>)}
    </div>

    <div className="mb-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
      {metricsCards(data.totals).map((item) => <MetricCard key={item.label} {...item}/>) }
    </div>

    <Card className="mb-5">
      <CardHeader><CardTitle className="flex items-center gap-2"><Megaphone size={17}/>الحملات · {data.period_label}</CardTitle></CardHeader>
      <CardContent>
        {data.campaigns.length ? <div className="space-y-3">
          {data.campaigns.map((campaign) => <Link key={campaign.campaign_id} href={`/analytics/campaigns/${campaign.campaign_id}`} className="block rounded-2xl border border-[var(--border)] p-4 transition hover:border-teal-300 hover:bg-teal-50/20">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><div className="font-black">{campaign.name}</div><div className="mt-1 text-xs text-[var(--muted)]">{campaign.confirmed_at ? new Date(campaign.confirmed_at).toLocaleString("ar-EG") : "—"}</div></div>
              <div className="text-left text-xs"><b>{campaign.sent_count.toLocaleString("ar-EG")}</b> إرسال · <b>{campaign.delivery_rate.toLocaleString("ar-EG")}%</b> وصول · <b>{campaign.tracked_booking_count.toLocaleString("ar-EG")}</b> حجز</div>
            </div>
          </Link>)}
        </div> : <div className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-[var(--muted)]">مفيش حملات مؤكدة في الفترة دي.</div>}
      </CardContent>
    </Card>

    <details className="rounded-2xl border border-[var(--border)] bg-white p-4">
      <summary className="cursor-pointer text-sm font-black">كيف نحسب النتائج؟</summary>
      <div className="mt-3 space-y-2 text-xs leading-6 text-[var(--muted)]">{data.definitions.map((definition) => <p key={definition}>• {definition}</p>)}</div>
    </details>
  </>;
}
