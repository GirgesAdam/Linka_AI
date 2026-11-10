import { PermissionLink as Link } from "@/components/permission-link";
import { ArrowLeft, CheckCheck, Eye, Send } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";
import type { CampaignAnalyticsOverview } from "@/lib/types";

function ProgressRow({ label, value, max, note }: { label:string; value:number; max:number; note:string }) {
  const width = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return <div>
    <div className="mb-1 flex items-center justify-between gap-3 text-xs"><b>{label}</b><span>{value.toLocaleString("ar-EG")} · {note}</span></div>
    <div className="h-2.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-slate-900" style={{ width:`${width}%` }}/></div>
  </div>;
}

export default async function CampaignDetailPage({ params }: { params:Promise<{campaignId:string}> }) {
  const { campaignId } = await params;
  const data = await tiaRequest<CampaignAnalyticsOverview>(`/analytics/campaigns/${campaignId}`);
  const campaign = data.campaigns[0];
  return <>
    <PageHeader
      title={campaign.name}
      description="تفاصيل الوصول والقراءة والحجوزات والإيراد المرتبط بهذه الحملة."
      action={<Link href="/analytics/campaigns" className="inline-flex items-center gap-2 rounded-xl border border-[var(--border)] bg-white px-3 py-2 text-sm font-bold"><ArrowLeft size={16}/>كل الحملات</Link>}
    />

    <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="rounded-2xl border border-[var(--border)] bg-white p-4"><div className="flex items-center justify-between text-xs text-[var(--muted)]"><b>تم الإرسال</b><Send size={16}/></div><div className="mt-2 text-2xl font-black">{campaign.sent_count.toLocaleString("ar-EG")}</div></div>
      <div className="rounded-2xl border border-[var(--border)] bg-white p-4"><div className="flex items-center justify-between text-xs text-[var(--muted)]"><b>معدل الوصول</b><CheckCheck size={16}/></div><div className="mt-2 text-2xl font-black">{campaign.delivery_rate.toLocaleString("ar-EG")}%</div></div>
      <div className="rounded-2xl border border-[var(--border)] bg-white p-4"><div className="flex items-center justify-between text-xs text-[var(--muted)]"><b>معدل القراءة</b><Eye size={16}/></div><div className="mt-2 text-2xl font-black">{campaign.read_rate.toLocaleString("ar-EG")}%</div></div>
      <div className="rounded-2xl border border-[var(--border)] bg-white p-4"><div className="text-xs text-[var(--muted)]"><b>إيراد منسوب</b></div><div className="mt-2 text-2xl font-black">{formatMoney(campaign.attributed_revenue_minor, campaign.currency)}</div></div>
    </div>

    <div className="mb-5 grid gap-5 lg:grid-cols-2">
      <Card><CardHeader><CardTitle>رحلة الرسالة</CardTitle></CardHeader><CardContent className="space-y-5">
        <ProgressRow label="تم الإرسال" value={campaign.sent_count} max={campaign.sent_count} note="100% من المرسل"/>
        <ProgressRow label="وصلت" value={campaign.delivered_count} max={campaign.sent_count} note={`${campaign.delivery_rate.toLocaleString("ar-EG")}% من المرسل`}/>
        <ProgressRow label="اتقرت" value={campaign.read_count} max={campaign.sent_count} note={`${campaign.read_rate.toLocaleString("ar-EG")}% من اللي وصل`}/>
        <div className="border-t border-[var(--border)] pt-3 text-xs text-[var(--muted)]">فشل: {campaign.failed_count.toLocaleString("ar-EG")} · إلغاء قبل/أثناء الإرسال: {campaign.cancelled_count.toLocaleString("ar-EG")}</div>
      </CardContent></Card>

      <Card><CardHeader><CardTitle>نتيجة تجارية متتبعة</CardTitle></CardHeader><CardContent className="space-y-4">
        <div className="rounded-xl bg-slate-50 p-4"><div className="text-xs text-[var(--muted)]">حجوزات منسوبة</div><div className="mt-1 text-3xl font-black">{campaign.tracked_booking_count.toLocaleString("ar-EG")}</div><div className="mt-1 text-xs text-[var(--muted)]">{campaign.booking_conversion_rate.toLocaleString("ar-EG")}% من الرسائل المرسلة</div></div>
        <div className="grid grid-cols-2 gap-3"><div className="rounded-xl border border-[var(--border)] p-3"><div className="text-xs text-[var(--muted)]">حجوزات اكتملت</div><div className="mt-1 text-xl font-black">{campaign.completed_booking_count.toLocaleString("ar-EG")}</div></div><div className="rounded-xl border border-[var(--border)] p-3"><div className="text-xs text-[var(--muted)]">الإيراد المنسوب</div><div className="mt-1 text-xl font-black">{formatMoney(campaign.attributed_revenue_minor, campaign.currency)}</div></div></div>
        <Link href={`/analytics/cohorts/${campaign.cohort_id}`} className="inline-flex text-sm font-bold text-teal-800 hover:underline">فتح مجموعة العملاء الأصلية</Link>
      </CardContent></Card>
    </div>

    <details className="rounded-2xl border border-[var(--border)] bg-white p-4"><summary className="cursor-pointer text-sm font-black">كيف نحسب النتائج؟</summary><div className="mt-3 space-y-2 text-xs leading-6 text-[var(--muted)]">{data.definitions.map((definition) => <p key={definition}>• {definition}</p>)}</div></details>
  </>;
}
