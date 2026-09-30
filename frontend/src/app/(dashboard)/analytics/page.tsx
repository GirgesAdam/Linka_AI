import Link from "next/link";
import { Bookmark, CalendarRange, Megaphone } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { tiaRequest } from "@/lib/tia/api";
import type { AnalyticsCatalog, AnalyticsSavedView, CRMCohort } from "@/lib/types";
import { AnalyticsCatalogPanel } from "./catalog";
import { AnalyticsOverviewPanel } from "./overview";

type SearchParams = { month?: string; year?: string };

function cairoTodayParts() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Africa/Cairo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return { year: Number(values.year), month: Number(values.month), day: Number(values.day) };
}

function isoDate(year: number, month: number, day: number) {
  return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}

function monthLabel(year: number, month: number) {
  return new Intl.DateTimeFormat("ar-EG", { month: "long", year: "numeric", timeZone: "Africa/Cairo" }).format(new Date(Date.UTC(year, month - 1, 15)));
}

function resolvePeriod(raw: SearchParams) {
  const today = cairoTodayParts();
  const requestedYear = /^\d{4}$/.test(raw.year || "") ? Number(raw.year) : null;
  if (requestedYear && requestedYear >= 2000 && requestedYear <= today.year) {
    return {
      startDate: isoDate(requestedYear, 1, 1),
      endDate: isoDate(requestedYear, 12, 31),
      label: `سنة ${requestedYear.toLocaleString("ar-EG", { useGrouping: false })}`,
      fullYear: true,
      selectedMonth: "",
      selectedYear: String(requestedYear),
      currentMonth: `${today.year}-${String(today.month).padStart(2, "0")}`,
      currentYear: today.year,
    };
  }

  const monthMatch = /^(\d{4})-(\d{2})$/.exec(raw.month || "");
  let year = today.year;
  let month = today.month;
  if (monthMatch) {
    const candidateYear = Number(monthMatch[1]);
    const candidateMonth = Number(monthMatch[2]);
    const notFuture = candidateYear < today.year || (candidateYear === today.year && candidateMonth <= today.month);
    if (candidateYear >= 2000 && candidateMonth >= 1 && candidateMonth <= 12 && notFuture) {
      year = candidateYear;
      month = candidateMonth;
    }
  }
  const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return {
    startDate: isoDate(year, month, 1),
    endDate: isoDate(year, month, lastDay),
    label: monthLabel(year, month),
    fullYear: false,
    selectedMonth: `${year}-${String(month).padStart(2, "0")}`,
    selectedYear: "",
    currentMonth: `${today.year}-${String(today.month).padStart(2, "0")}`,
    currentYear: today.year,
  };
}

export default async function AnalyticsPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const raw = await searchParams;
  const period = resolvePeriod(raw);
  const [cohorts, catalog, savedViews] = await Promise.all([
    tiaRequest<CRMCohort[]>("/crm/cohorts?limit=8"),
    tiaRequest<AnalyticsCatalog>("/analytics/catalog"),
    tiaRequest<AnalyticsSavedView[]>("/analytics/views?limit=20"),
  ]);
  const visibleCatalog: AnalyticsCatalog = {
    ...catalog,
    analyses: catalog.analyses.filter((analysis) => analysis.category !== "doctors"),
    doctors: [],
  };
  const visibleSavedViews = savedViews.filter((view) => visibleCatalog.analyses.some((analysis) => analysis.key === view.analysis_key));
  const years = Array.from({ length: Math.max(1, period.currentYear - 2019) }, (_, index) => period.currentYear - index);

  return (
    <>
      <PageHeader
        title="التقارير والتحليلات"
        description="راجع الأداء المالي والتشغيلي في فترة واضحة، ثم افتح التقرير التفصيلي الذي تحتاجه."
        action={<Link href="/analytics/campaigns" className="inline-flex items-center gap-2 rounded-xl border border-[var(--border)] bg-white px-3 py-2 text-sm font-bold"><Megaphone size={16}/>أداء الحملات</Link>}
      />

      <Card className="mb-6 overflow-hidden">
        <CardContent className="flex flex-wrap items-end gap-3 p-4 sm:p-4">
          <div className="ml-auto">
            <div className="flex items-center gap-2 text-sm font-black text-slate-900"><CalendarRange size={18} /> الفترة</div>
            <div className="mt-1 text-[11px] text-[var(--muted)]">الملخص العلوي يتغير حسب الشهر أو السنة المختارة.</div>
          </div>
          <form method="get" className="flex flex-wrap items-end gap-2">
            <label className="text-xs font-bold text-slate-600">اختر شهرًا
              <input type="month" name="month" defaultValue={period.selectedMonth || period.currentMonth} max={period.currentMonth} className="form-control mt-1 min-w-[175px]" />
            </label>
            <Button type="submit" variant={period.fullYear ? "outline" : "default"}>عرض الشهر</Button>
          </form>
          <span className="hidden h-9 w-px bg-slate-200 sm:block" />
          <form method="get" className="flex items-end gap-2">
            <label className="text-xs font-bold text-slate-600">أو سنة كاملة
              <select name="year" defaultValue={period.selectedYear || String(period.currentYear)} className="form-control mt-1 min-w-[130px]">
                {years.map((year) => <option key={year} value={year}>{year.toLocaleString("ar-EG", { useGrouping: false })}</option>)}
              </select>
            </label>
            <Button type="submit" variant={period.fullYear ? "default" : "outline"}>عرض السنة</Button>
          </form>
        </CardContent>
      </Card>

      <AnalyticsOverviewPanel
        startDate={period.startDate}
        endDate={period.endDate}
        periodLabel={period.label}
        fullYear={period.fullYear}
      />

      <section>
        <div className="mb-3">
          <h2 className="text-lg font-black text-slate-950">التقارير التفصيلية</h2>
          <p className="mt-1 text-sm text-[var(--muted)]">اختر التقرير المناسب للسؤال الذي تريد إجابته، واضبط فترته وفلاتره عند الحاجة.</p>
        </div>

        <AnalyticsCatalogPanel catalog={visibleCatalog} savedViews={visibleSavedViews} />
      </section>

      <Card className="mb-5">
        <CardHeader className="flex-row items-center justify-between gap-3">
          <div>
            <CardTitle>مجموعات العملاء المحفوظة</CardTitle>
            <p className="mt-1 text-xs text-[var(--muted)]">قوائم حفظتها سابقًا لاستخدامها في المتابعة أو الحملات.</p>
          </div>
          <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-slate-100 text-slate-600"><Bookmark size={17} /></span>
        </CardHeader>
        <CardContent>
          {cohorts.length ? (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {cohorts.map((cohort) => (
                <Link key={cohort.id} href={`/analytics/cohorts/${cohort.id}`} className="rounded-2xl border border-[var(--border)] p-3 transition hover:border-[var(--accent-border)] hover:bg-[var(--accent-soft)]">
                  <div className="font-black">{cohort.name}</div>
                  <div className="mt-1 text-xs text-[var(--muted)]">{cohort.member_count.toLocaleString("ar-EG")} عميل · {cohort.period_label}</div>
                </Link>
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-[var(--muted)]">لا توجد مجموعات محفوظة بعد.</div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
