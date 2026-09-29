"use client";

import { useActionState, useEffect, useMemo, useRef, useState } from "react";
import { BarChart3, BookmarkPlus, Download, Filter, Info, LoaderCircle, Play, Search, SlidersHorizontal } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatMoney } from "@/lib/format";
import type {
  AnalyticsBIMetric,
  AnalyticsCatalog,
  AnalyticsCatalogCategory,
  AnalyticsCatalogChart,
  AnalyticsCatalogChartSeries,
  AnalyticsCatalogDefinition,
  AnalyticsCatalogRun,
  AnalyticsSavedView,
} from "@/lib/types";
import {
  runAnalyticsCatalogAction,
  saveAnalyticsViewAction,
  type AnalyticsCatalogState,
  type AnalyticsSavedViewState,
} from "./actions";

const initialState: AnalyticsCatalogState = { result: null, error: null };
const initialSavedViewState: AnalyticsSavedViewState = { view: null, error: null };

type AnalyticsCategoryGroup = "performance" | "customers" | "team";
type VisualAnalyticsChart = Exclude<AnalyticsCatalogChart, "table">;

const categoryLabels: Partial<Record<AnalyticsCatalogCategory, string>> = {
  revenue: "الإيرادات",
  patients: "العملاء",
  appointments: "المواعيد",
  doctors: "الدكاترة",
  retention: "الاحتفاظ بالعملاء",
  funnels: "رحلة العميل",
};

const categoryGroups: Array<{ key: AnalyticsCategoryGroup; label: string; description: string; categories: AnalyticsCatalogCategory[] }> = [
  { key: "performance", label: "الأداء", description: "الإيرادات والمواعيد", categories: ["revenue", "appointments"] },
  { key: "customers", label: "العملاء", description: "النمو والعودة والتحويل", categories: ["patients", "retention", "funnels"] },
  { key: "team", label: "الدكاترة", description: "أداء الفريق الطبي", categories: ["doctors"] },
];

const chartLabels: Partial<Record<AnalyticsCatalogChart, string>> = {
  kpi: "بطاقات",
  line: "خط",
  bar: "أعمدة",
  heatmap: "خريطة حرارية",
  funnel: "مسار",
};

const quickAccessKeys = ["revenue_overview", "appointment_overview", "revenue_by_doctor", "new_patients_trend"] as const;

const REPORT_GUIDES: Record<string, { benefit: string; calculation: string }> = {
  revenue_overview: {
    benefit: "يعطيك صورة مالية سريعة عن حجم التحصيل والمرتجعات وصافي الدخل في الفترة، لتعرف هل الأداء المالي يتحسن أم لا.",
    calculation: "يجمع المدفوعات المسجلة خلال الفترة، ويطرح منها المرتجعات المسجلة. كل رقم يأتي من سجل المدفوعات الفعلي في Linka.",
  },
  revenue_trend: {
    benefit: "يوضح اتجاه الدخل عبر الوقت، ويساعدك تكتشف الأيام أو الشهور الأقوى والأضعف وتربطها بقرارات التشغيل والتسويق.",
    calculation: "يحسب صافي المدفوعات لكل فترة زمنية: المدفوعات المسجلة ناقص المرتجعات، ثم يعرضها يوميًا أو أسبوعيًا أو شهريًا.",
  },
  revenue_by_doctor: {
    benefit: "يساعدك تفهم مساهمة كل دكتور في الدخل المسجل ومقارنة حجم النشاط المالي بين أعضاء الفريق.",
    calculation: "ينسب صافي المدفوعات للمواعيد المرتبطة بكل دكتور فقط، ثم يجمعها داخل الفترة المختارة.",
  },
  average_patient_value: {
    benefit: "يوضح متوسط قيمة العميل الذي دفع فعليًا، ويفيد في تقدير قيمة الاحتفاظ بالعميل وتكلفة اكتسابه المقبولة.",
    calculation: "يقسم صافي المدفوعات المسجلة على عدد العملاء الذين لديهم دفعة فعلية خلال الفترة.",
  },
  new_patients_trend: {
    benefit: "يوضح هل قاعدة العملاء الجدد تنمو أم تتراجع، ويساعدك تقيس تأثير الحملات والمواسم على اكتساب العملاء.",
    calculation: "يعد العملاء حسب تاريخ انضمامهم الأصلي عند توفره، وإلا يستخدم تاريخ إنشاء ملفهم في Linka، ثم يجمعهم حسب الفترة الزمنية.",
  },
  repeat_patient_rate: {
    benefit: "يقيس قدرة العيادة على إعادة العميل مرة أخرى بدل الاعتماد الدائم على عملاء جدد.",
    calculation: "يحسب نسبة العملاء الذين لديهم جلستان مكتملتان أو أكثر خلال الفترة إلى إجمالي العملاء الذين لديهم جلسات مكتملة.",
  },
  appointment_overview: {
    benefit: "يعطيك ملخصًا تشغيليًا لحجم المواعيد وما انتهى منها بإتمام أو إلغاء أو عدم حضور.",
    calculation: "يعد المواعيد المسجلة في الفترة حسب حالتها الفعلية، ويحسب النسب من نفس مجموعة المواعيد بدون تقدير من الـAI.",
  },
  appointment_trend: {
    benefit: "يكشف تغير ضغط المواعيد والجلسات المكتملة مع الوقت لتخطيط السعة والمواعيد بشكل أفضل.",
    calculation: "يجمع المواعيد حسب اليوم أو الأسبوع أو الشهر، ثم يحسب لكل فترة عدد المواعيد والجلسات المكتملة وعدم الحضور والإلغاء.",
  },
  doctor_appointment_performance: {
    benefit: "يساعدك تقارن أداء الدكاترة من ناحية حجم المواعيد والإتمام وعدم الحضور، وليس فقط الإيراد.",
    calculation: "يجمع مواعيد كل دكتور ثم يحسب نسب الإتمام وعدم الحضور والإلغاء من حالات المواعيد المسجلة.",
  },
  appointment_source_performance: {
    benefit: "يوضح أي مصدر حجز يجلب مواعيد أكثر وأفضل جودة، مثل واتساب أو الهاتف أو الويب.",
    calculation: "يجمع المواعيد حسب مصدر الحجز المسجل ويحسب لكل مصدر الحجم ونسب الإتمام وعدم الحضور والإلغاء.",
  },
  appointment_peak_weekdays: {
    benefit: "يساعدك تعرف أيام الأسبوع الأكثر ضغطًا لتوزيع الطاقم والساعات المتاحة بشكل أفضل.",
    calculation: "يحوّل وقت كل موعد إلى توقيت العيادة، ثم يجمع عدد المواعيد حسب يوم الأسبوع داخل الفترة.",
  },
  appointment_peak_hours: {
    benefit: "يكشف ساعات الذروة خلال أيام الأسبوع لتقليل الزحام وتحسين توزيع السعة.",
    calculation: "يجمع المواعيد حسب يوم الأسبوع وساعة البداية بتوقيت العيادة، ثم يعرض الكثافة في خريطة حرارية.",
  },
  no_show_peak_times: {
    benefit: "يساعدك تحدد الأوقات التي يكثر فيها عدم الحضور لتشديد التذكيرات أو تعديل سياسة التأكيد فيها.",
    calculation: "لكل يوم وساعة، يقسم عدد مواعيد عدم الحضور على المواعيد التي انتهت بإتمام أو عدم حضور في نفس الخانة الزمنية.",
  },
  cancellation_peak_times: {
    benefit: "يكشف الأوقات الأكثر تعرضًا للإلغاء حتى تراجع سياسة التأكيد أو طريقة ملء المواعيد البديلة.",
    calculation: "يجمع الإلغاءات والمواعيد حسب يوم وساعة الموعد ثم يحسب نسبة الإلغاء لكل خانة زمنية.",
  },
  doctor_retention: {
    benefit: "يوضح قدرة كل دكتور على جعل عملائه يعودون لزيارة أخرى معه.",
    calculation: "لكل دكتور، يحسب نسبة العملاء الذين عادوا لجلسة مكتملة ثانية مع نفس الدكتور داخل الفترة.",
  },
  second_visit_conversion: {
    benefit: "يقيس نسبة العملاء الذين تحولت تجربتهم الأولى إلى زيارة ثانية، وهي إشارة مهمة للاحتفاظ المبكر.",
    calculation: "من العملاء الذين أكملوا زيارة، يحسب من منهم وصل إلى جلستين مكتملتين داخل الفترة المختارة.",
  },
  third_visit_conversion: {
    benefit: "يقيس التحول من تجربة أولى إلى علاقة أكثر استمرارية مع العيادة.",
    calculation: "من العملاء الذين أكملوا زيارة، يحسب من منهم وصل إلى ثلاث جلسات مكتملة داخل الفترة.",
  },
  time_to_return: {
    benefit: "يعرفك المدة المعتادة قبل رجوع العميل، فتقدر تضبط توقيت المتابعة والتذكير بإعادة الحجز.",
    calculation: "يحسب عدد الأيام بين أول وثاني جلسة مكتملة لكل عميل، ثم يعرض المتوسط والوسيط للمدد.",
  },
  lapsed_rate: {
    benefit: "يوضح حجم العملاء الذين ابتعدوا عن العيادة ويحتاجون خطة استرجاع أو متابعة.",
    calculation: "يعتبر العميل منقطعًا إذا تجاوزت آخر جلسة مكتملة له مدة الانقطاع المحددة ولم يكن لديه حجز نشط قادم، ثم يحسب نسبتهم.",
  },
  booking_completion_funnel: {
    benefit: "يوضح كم من الحجوزات يتحول فعليًا إلى جلسات مكتملة ويكشف التسرب بين الحجز والحضور.",
    calculation: "يبدأ بعدد المواعيد المسجلة ثم يقارنه بعدد الجلسات التي وصلت إلى حالة مكتمل داخل نفس الفترة.",
  },
  booking_paid_funnel: {
    benefit: "يربط الحجز بالإتمام ثم بالدفع لتعرف أين يحدث أكبر تسرب في الرحلة المالية للموعد.",
    calculation: "يعرض المواعيد ثم الجلسات المكتملة ثم الجلسات المكتملة التي لها دفعة مسجلة فعليًا، ويحسب التحويل بين المراحل.",
  },
};

function guideFor(item: Pick<AnalyticsCatalogDefinition, "key" | "description">) {
  return REPORT_GUIDES[item.key] || {
    benefit: `استخدم هذا التقرير لفهم ${item.description.replace(/\.$/, "")} واتخاذ قرار تشغيلي مبني على نفس الأرقام كل مرة.`,
    calculation: `يُحسب من البيانات المسجلة داخل Linka وفق تعريف التقرير: ${item.description}`,
  };
}

function isVisibleDetailedReport(item: AnalyticsCatalogDefinition) {
  if (item.category === "services" || item.category === "branches") return false;
  if (item.key.includes("service") || item.key.includes("branch")) return false;
  if (item.result_kind === "patient_list") return false;
  return item.supported_charts.some((chart) => chart !== "table");
}

function hasFilter(definition: AnalyticsCatalogDefinition, key: AnalyticsCatalogDefinition["filters"][number]) {
  return definition.filters.includes(key);
}

function metricValue(metric: AnalyticsBIMetric) {
  if (metric.currency && typeof metric.value === "number") return formatMoney(metric.value, metric.currency);
  if (typeof metric.value === "number" && (metric.key.includes("rate") || metric.key.includes("percent") || metric.key.includes("change_percent"))) {
    return `${metric.value.toLocaleString("ar-EG", { maximumFractionDigits: 1 })}%`;
  }
  if (typeof metric.value === "number") return metric.value.toLocaleString("ar-EG", { maximumFractionDigits: 1 });
  return metric.value;
}

function seriesValue(series: AnalyticsCatalogChartSeries, value: number | null) {
  if (value === null) return "—";
  if (series.format === "money") return formatMoney(value, series.currency || "EGP");
  if (series.format === "percent") return `${value.toLocaleString("ar-EG", { maximumFractionDigits: 1 })}%`;
  return value.toLocaleString("ar-EG", { maximumFractionDigits: 1 });
}

function MetricCards({ metrics }: { metrics: AnalyticsBIMetric[] }) {
  const visible = metrics.filter((metric) => !metric.key.endsWith("_previous") && !metric.key.endsWith("_change_percent") && !metric.key.endsWith("_delta_points"));
  if (!visible.length) return null;
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {visible.map((metric) => (
        <div key={metric.key} className="rounded-2xl border border-[var(--border)] bg-slate-50 p-4">
          <div className="text-xs text-[var(--muted)]">{metric.label}</div>
          <div className="mt-2 text-2xl font-black">{metricValue(metric)}</div>
        </div>
      ))}
    </div>
  );
}

function BarVisualization({ result, series }: { result: AnalyticsCatalogRun; series: AnalyticsCatalogChartSeries }) {
  const values = series.values.map((value) => value ?? 0);
  const max = Math.max(1, ...values.map((value) => Math.abs(value)));
  return (
    <div className="space-y-3">
      <div className="text-xs font-bold text-[var(--muted)]">{series.label}</div>
      {result.chart_data.labels.slice(0, 20).map((label, index) => {
        const value = values[index] || 0;
        const width = value === 0 ? 0 : Math.max(2, Math.abs(value) / max * 100);
        return (
          <div key={`${label}-${index}`} className="grid grid-cols-[minmax(110px,190px)_1fr_auto] items-center gap-3 text-xs">
            <span className="truncate font-bold" title={label}>{label}</span>
            <div className="h-3 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-teal-700" style={{ width: `${width}%` }} /></div>
            <span className="min-w-20 text-left font-black">{seriesValue(series, series.values[index] ?? null)}</span>
          </div>
        );
      })}
    </div>
  );
}

function LineVisualization({ result, series }: { result: AnalyticsCatalogRun; series: AnalyticsCatalogChartSeries }) {
  if (!series.values.length) return null;
  const values = series.values.map((value) => value ?? 0);
  const width = 760;
  const height = 240;
  const padding = 26;
  const min = Math.min(0, ...values);
  const max = Math.max(1, ...values);
  const span = Math.max(1, max - min);
  const coordinates = values.map((value, index) => ({
    x: values.length === 1 ? width / 2 : padding + index * ((width - padding * 2) / (values.length - 1)),
    y: height - padding - ((value - min) / span) * (height - padding * 2),
  }));
  return (
    <div>
      <div className="mb-2 text-xs font-bold text-[var(--muted)]">{series.label}</div>
      <div className="overflow-x-auto">
        <svg viewBox={`0 0 ${width} ${height}`} className="h-60 min-w-[620px] w-full" role="img" aria-label={`${result.title} chart`}>
          <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} className="stroke-slate-200" />
          <polyline points={coordinates.map((point) => `${point.x},${point.y}`).join(" ")} fill="none" className="stroke-teal-700" strokeWidth="4" strokeLinejoin="round" strokeLinecap="round" />
          {coordinates.map((point, index) => <circle key={index} cx={point.x} cy={point.y} r="4" className="fill-white stroke-teal-700" strokeWidth="3"><title>{`${result.chart_data.labels[index] || ""}: ${seriesValue(series, series.values[index] ?? null)}`}</title></circle>)}
        </svg>
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-[var(--muted)]"><span>{result.chart_data.labels[0] || "—"}</span><span>{result.chart_data.labels.at(-1) || "—"}</span></div>
    </div>
  );
}

function HeatmapVisualization({ result }: { result: AnalyticsCatalogRun }) {
  const values = result.chart_data.series.flatMap((series) => series.values.map((value) => value ?? 0));
  const max = Math.max(1, ...values.map((value) => Math.abs(value)));
  return (
    <div className="overflow-x-auto">
      <div className="min-w-max">
        <div className="grid gap-1" style={{ gridTemplateColumns: `90px repeat(${result.chart_data.labels.length}, minmax(58px, 1fr))` }}>
          <div />
          {result.chart_data.labels.map((label) => <div key={label} className="px-1 py-2 text-center text-[10px] font-bold text-[var(--muted)]">{label}</div>)}
          {result.chart_data.series.map((series) => (
            <div key={series.key} className="contents">
              <div className="flex items-center px-2 text-xs font-black">{series.label}</div>
              {series.values.map((raw, index) => {
                const value = raw ?? 0;
                const intensity = Math.max(0.06, Math.abs(value) / max);
                return <div key={`${series.key}-${index}`} className="rounded-lg border border-teal-100 px-1 py-3 text-center text-[10px] font-black text-teal-950" style={{ backgroundColor: `rgb(204 251 241 / ${intensity})` }} title={`${series.label} · ${result.chart_data.labels[index] || ""}: ${seriesValue(series, raw ?? null)}`}>{seriesValue(series, raw ?? null)}</div>;
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function FunnelVisualization({ result }: { result: AnalyticsCatalogRun }) {
  const series = result.chart_data.series[0];
  if (!series) return null;
  const values = series.values.map((value) => value ?? 0);
  const first = Math.max(0, values[0] || 0);
  return (
    <div className="mx-auto max-w-3xl space-y-3">
      {result.chart_data.labels.map((label, index) => {
        const value = values[index] || 0;
        const ratio = first > 0 ? Math.max(0, Math.min(100, value / first * 100)) : 0;
        const previous = index > 0 ? values[index - 1] || 0 : first;
        const fromPrevious = index > 0 && previous > 0 ? Math.max(0, Math.min(100, value / previous * 100)) : 100;
        return (
          <div key={`${label}-${index}`} className="rounded-2xl border border-[var(--border)] bg-slate-50 p-4">
            <div className="flex items-end justify-between gap-3">
              <div><div className="text-xs text-[var(--muted)]">{label}</div><div className="mt-1 text-xl font-black">{seriesValue(series, series.values[index] ?? null)}</div></div>
              {index > 0 && <div className="text-left text-[11px] text-[var(--muted)]"><div><span className="font-black text-slate-800">{fromPrevious.toLocaleString("ar-EG", { maximumFractionDigits: 1 })}%</span> من المرحلة السابقة</div><div>{ratio.toLocaleString("ar-EG", { maximumFractionDigits: 1 })}% من البداية</div></div>}
            </div>
            <div className="mt-3 h-3 overflow-hidden rounded-full bg-slate-200"><div className="h-full rounded-full bg-teal-700" style={{ width: `${ratio}%` }} /></div>
          </div>
        );
      })}
    </div>
  );
}

async function downloadCsv(result: AnalyticsCatalogRun) {
  const response = await fetch("/api/analytics/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(result.request),
  });
  if (!response.ok) throw new Error("حصل خطأ أثناء تجهيز ملف CSV.");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `tia-${result.analysis_key}.csv`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function SaveViewControl({ result, chart }: { result: AnalyticsCatalogRun; chart: VisualAnalyticsChart }) {
  const [state, action, pending] = useActionState<AnalyticsSavedViewState, FormData>(saveAnalyticsViewAction, initialSavedViewState);
  const suggestedName = `${result.title} — ${result.period_label}`.slice(0, 160);
  return (
    <details className="relative">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-xl border border-[var(--border)] bg-white px-3 py-2 text-xs font-bold"><BookmarkPlus size={16} /> حفظ العرض</summary>
      <form action={action} className="absolute left-0 z-20 mt-2 w-72 rounded-2xl border border-[var(--border)] bg-white p-3 shadow-xl">
        <input type="hidden" name="request" value={JSON.stringify(result.request)} />
        <input type="hidden" name="chart" value={chart} />
        <input type="hidden" name="display_mode" value="visual" />
        <label className="text-xs font-black">اسم العرض<input name="name" defaultValue={suggestedName} maxLength={160} className="form-control mt-1" /></label>
        <p className="mt-2 text-[11px] leading-5 text-[var(--muted)]">هنحفظ التقرير والفلاتر وطريقة الرسم فقط؛ الأرقام تتحدث كل مرة تفتحه.</p>
        <Button type="submit" className="mt-3 w-full" disabled={pending}>{pending ? <LoaderCircle size={15} className="animate-spin" /> : <BookmarkPlus size={15} />} حفظ</Button>
        {state.view && <div className="mt-2 text-[11px] font-bold text-emerald-700">اتحفظ باسم «{state.view.name}».</div>}
        {state.error && <div className="mt-2 text-[11px] font-bold text-red-700">{state.error}</div>}
      </form>
    </details>
  );
}

function ResultPanel({ result }: { result: AnalyticsCatalogRun }) {
  const visualCharts = result.supported_charts.filter((chart): chart is VisualAnalyticsChart => chart !== "table");
  const initial: VisualAnalyticsChart = result.chart !== "table" && visualCharts.includes(result.chart) ? result.chart : visualCharts[0] || "kpi";
  const [chartType, setChartType] = useState<VisualAnalyticsChart>(initial);
  const [seriesKey, setSeriesKey] = useState(result.chart_data.series[0]?.key || "");
  const [exportPending, setExportPending] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const activeSeries = result.chart_data.series.find((series) => series.key === seriesKey) || result.chart_data.series[0];
  const guide = REPORT_GUIDES[result.analysis_key];

  if (!result.rows.length) return <div className="mt-6 rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-8 text-center"><div className="font-black">مفيش بيانات مطابقة للفترة دي</div><p className="mt-2 text-sm text-[var(--muted)]">غيّر الفترة وشغّل التقرير مرة أخرى.</p>{result.definitions.length > 0 && <ul className="mx-auto mt-4 max-w-2xl list-disc space-y-1 pr-5 text-right text-xs leading-6 text-slate-600">{result.definitions.map((item, index) => <li key={index}>{item}</li>)}</ul>}</div>;

  return (
    <div className="mt-6 space-y-4 border-t border-[var(--border)] pt-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h3 className="text-lg font-black">{result.title}</h3><div className="mt-1 text-xs text-[var(--muted)]">{result.period_label}</div></div>
        <div className="flex flex-wrap gap-2">
          <SaveViewControl result={result} chart={chartType} />
          <Button type="button" variant="outline" disabled={exportPending} onClick={async () => { setExportPending(true); setExportError(null); try { await downloadCsv(result); } catch (error) { setExportError(error instanceof Error ? error.message : "تعذر التصدير."); } finally { setExportPending(false); } }}>{exportPending ? <LoaderCircle size={16} className="animate-spin" /> : <Download size={16} />} تصدير CSV</Button>
        </div>
      </div>
      {exportError && <div className="rounded-xl bg-red-50 p-3 text-xs font-bold text-red-700">{exportError}</div>}

      {result.highlights.length > 0 && <MetricCards metrics={result.highlights} />}

      <div className="rounded-2xl border border-[var(--border)] p-4">
        {(visualCharts.length > 1 || (result.chart_data.series.length > 1 && ["bar", "line"].includes(chartType))) && (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] pb-3">
            <div className="flex flex-wrap gap-2">{visualCharts.map((chart) => <button key={chart} type="button" onClick={() => setChartType(chart)} className={`rounded-lg px-3 py-1.5 text-xs font-bold ${chartType === chart ? "bg-teal-50 text-teal-800" : "bg-slate-50 text-slate-600"}`}>{chartLabels[chart] || "الرسم"}</button>)}</div>
            {result.chart_data.series.length > 1 && ["bar", "line"].includes(chartType) && <select value={activeSeries?.key || ""} onChange={(event) => setSeriesKey(event.target.value)} className="rounded-lg border border-[var(--border)] bg-white px-3 py-1.5 text-xs font-bold">{result.chart_data.series.map((series) => <option key={series.key} value={series.key}>{series.label}</option>)}</select>}
          </div>
        )}
        {chartType === "kpi" && <MetricCards metrics={result.rows[0]?.metrics || []} />}
        {chartType === "bar" && activeSeries && <BarVisualization result={result} series={activeSeries} />}
        {chartType === "line" && activeSeries && <LineVisualization result={result} series={activeSeries} />}
        {chartType === "heatmap" && <HeatmapVisualization result={result} />}
        {chartType === "funnel" && <FunnelVisualization result={result} />}
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <div className="rounded-2xl border border-teal-100 bg-teal-50/50 p-4"><div className="flex items-center gap-2 text-sm font-black text-teal-950"><Info size={16} /> تستفيد منه إزاي؟</div><p className="mt-2 text-xs leading-6 text-teal-950/80">{guide?.benefit || "استخدم اتجاه الرسم والمقارنات لتحديد التغيرات المهمة واتخاذ قرار تشغيلي بناءً على البيانات المسجلة."}</p></div>
        <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4"><div className="text-sm font-black text-slate-900">بيتحسب إزاي؟</div><p className="mt-2 text-xs leading-6 text-slate-700">{guide?.calculation || "الحساب يتم من البيانات المسجلة في Linka وبنفس التعريف كل مرة."}</p>{result.definitions.length > 0 && <ul className="mt-2 list-disc space-y-1 pr-5 text-[11px] leading-5 text-slate-600">{result.definitions.map((item, index) => <li key={index}>{item}</li>)}</ul>}</div>
      </div>
    </div>
  );
}

export function AnalyticsCatalogPanel({ catalog, savedViews }: { catalog: AnalyticsCatalog; savedViews: AnalyticsSavedView[] }) {
  const analyses = useMemo(() => catalog.analyses.filter(isVisibleDetailedReport), [catalog.analyses]);
  const groups = useMemo(() => categoryGroups.filter((group) => group.categories.some((category) => analyses.some((analysis) => analysis.category === category))), [analyses]);
  const [groupKey, setGroupKey] = useState<AnalyticsCategoryGroup>(groups[0]?.key || "performance");
  const [query, setQuery] = useState("");
  const [selectedKey, setSelectedKey] = useState(analyses[0]?.key || "");
  const [preset, setPreset] = useState<AnalyticsSavedView | null>(null);
  const [dirty, setDirty] = useState(true);
  const [state, action, pending] = useActionState<AnalyticsCatalogState, FormData>(runAnalyticsCatalogAction, initialState);
  const resultRef = useRef<HTMLDivElement>(null);

  const normalizedQuery = query.trim().toLocaleLowerCase("ar");
  const visibleAnalyses = useMemo(() => analyses.filter((item) => {
    if (normalizedQuery) return `${item.title} ${item.description} ${categoryLabels[item.category] || ""}`.toLocaleLowerCase("ar").includes(normalizedQuery);
    const active = groups.find((group) => group.key === groupKey) || groups[0];
    return Boolean(active?.categories.includes(item.category));
  }), [analyses, groupKey, groups, normalizedQuery]);
  const selected = visibleAnalyses.find((item) => item.key === selectedKey) || visibleAnalyses[0];
  const presetRequest = preset && selected && preset.analysis_key === selected.key ? preset.request : null;
  const result = selected && !dirty && !pending && state.result?.analysis_key === selected.key ? state.result : null;
  const quickAccess = quickAccessKeys.map((key) => analyses.find((item) => item.key === key)).filter((item): item is AnalyticsCatalogDefinition => Boolean(item));
  const hasEntityFilters = selected ? hasFilter(selected, "doctor") : false;

  useEffect(() => { if (result) resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [result]);

  if (!analyses.length) return <div className="rounded-2xl border border-[var(--border)] p-6 text-sm text-[var(--muted)]">لا توجد تقارير بصرية متاحة.</div>;

  const periodDefault = presetRequest?.all_history ? "all" : String(presetRequest?.lookback_days ?? selected?.default_lookback_days ?? "all");
  const guide = selected ? guideFor(selected) : null;

  return (
    <section className="mb-6 rounded-3xl border border-[var(--border)] bg-white p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3"><span className="grid size-11 place-items-center rounded-2xl bg-teal-700 text-white"><BarChart3 size={21} /></span><div><h2 className="text-lg font-black">التقارير</h2><p className="mt-1 text-sm leading-6 text-slate-600">التقارير هنا جرافات وبطاقات فقط. تقارير الفروع والخدمات والنتائج الجدولية تم إزالتها من هذه الصفحة.</p></div></div>
        <label className="relative w-full max-w-sm"><Search size={16} className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-400" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="ابحث عن تقرير" className="form-control pr-9" /></label>
      </div>

      {quickAccess.length > 0 && <div className="mt-4 flex flex-wrap gap-2"><span className="py-2 text-xs font-black text-slate-500">وصول سريع:</span>{quickAccess.map((item) => <button key={item.key} type="button" onClick={() => { setSelectedKey(item.key); setPreset(null); setDirty(true); const group = groups.find((candidate) => candidate.categories.includes(item.category)); if (group) setGroupKey(group.key); }} className="rounded-xl border border-[var(--border)] px-3 py-2 text-xs font-bold hover:border-teal-300">{item.title}</button>)}</div>}

      {savedViews.length > 0 && <div className="mt-3 flex flex-wrap gap-2"><span className="py-2 text-xs font-black text-slate-500">محفوظة:</span>{savedViews.map((view) => {
        const definition = analyses.find((item) => item.key === view.analysis_key);
        if (!definition) return null;
        return <button key={view.id} type="button" onClick={() => { setSelectedKey(definition.key); setPreset(view); setDirty(true); const group = groups.find((candidate) => candidate.categories.includes(definition.category)); if (group) setGroupKey(group.key); }} className="rounded-xl border border-[var(--border)] px-3 py-2 text-xs font-bold hover:border-teal-300">{view.name}</button>;
      })}</div>}

      {!normalizedQuery && <div className="mt-5 grid gap-2 sm:grid-cols-3">{groups.map((group) => <button key={group.key} type="button" onClick={() => { setGroupKey(group.key); setQuery(""); setPreset(null); setDirty(true); const first = analyses.find((item) => group.categories.includes(item.category)); if (first) setSelectedKey(first.key); }} className={`rounded-2xl border p-3 text-right ${groupKey === group.key ? "border-teal-600 bg-teal-50" : "border-[var(--border)]"}`}><div className="text-sm font-black">{group.label}</div><div className="mt-1 text-[11px] text-[var(--muted)]">{group.description}</div></button>)}</div>}

      <div className="mt-5 grid gap-5 xl:grid-cols-[.85fr_1.15fr]">
        <div className="grid content-start gap-2">{visibleAnalyses.map((item) => <button key={item.key} type="button" onClick={() => { setSelectedKey(item.key); setPreset(null); setDirty(true); const group = groups.find((candidate) => candidate.categories.includes(item.category)); if (group) setGroupKey(group.key); }} className={`rounded-2xl border p-4 text-right transition ${selected?.key === item.key ? "border-teal-500 bg-teal-50/60" : "border-[var(--border)] hover:border-teal-200"}`}><div className="font-black">{item.title}</div><div className="mt-1 text-xs leading-5 text-[var(--muted)]">{guideFor(item).benefit}</div></button>)}</div>

        {selected && <form key={`${selected.key}-${preset?.id || "fresh"}`} action={action} onSubmit={() => setDirty(false)} className="h-fit rounded-2xl border border-[var(--border)] bg-slate-50 p-4">
          <input type="hidden" name="analysis_key" value={selected.key} />
          <div className="flex items-start gap-2"><Filter size={17} className="mt-0.5" /><div><div className="font-black">{selected.title}</div><div className="mt-1 text-xs leading-5 text-[var(--muted)]">{selected.description}</div></div></div>
          {guide && <div className="mt-4 grid gap-2"><div className="rounded-xl bg-teal-50 p-3 text-xs leading-5 text-teal-950"><strong>تستفيد منه إزاي؟</strong> {guide.benefit}</div><div className="rounded-xl bg-white p-3 text-xs leading-5 text-slate-700"><strong>بيتحسب إزاي؟</strong> {guide.calculation}</div></div>}

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {hasFilter(selected, "period") && <label className="text-xs font-bold">الفترة<select name="period" defaultValue={periodDefault} className="form-control mt-1"><option value="7">آخر 7 أيام</option><option value="30">آخر 30 يوم</option><option value="90">آخر 90 يوم</option><option value="180">آخر 6 شهور</option><option value="365">آخر سنة</option><option value="730">آخر سنتين</option><option value="all">كل التاريخ</option></select></label>}
            {hasEntityFilters && <label className="text-xs font-bold">الدكتور (اختياري)<select name="doctor_id" defaultValue={presetRequest?.doctor_ids[0] || ""} className="form-control mt-1"><option value="">كل الدكاترة</option>{catalog.doctors.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
          </div>

          {(hasFilter(selected, "granularity") || hasFilter(selected, "limit") || hasFilter(selected, "comparison")) && <details className="mt-3 rounded-xl border border-[var(--border)] bg-white p-3"><summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-black"><SlidersHorizontal size={15} /> خيارات إضافية</summary><div className="mt-3 grid gap-3 md:grid-cols-2">{hasFilter(selected, "granularity") && <label className="text-xs font-bold">تجميع الفترة<select name="granularity" defaultValue={presetRequest?.granularity || selected.default_granularity || "month"} className="form-control mt-1"><option value="day">يومي</option><option value="week">أسبوعي</option><option value="month">شهري</option></select></label>}{hasFilter(selected, "limit") && <label className="text-xs font-bold">عدد العناصر<select name="limit" defaultValue={String(presetRequest?.limit ?? selected.default_limit)} className="form-control mt-1"><option value="5">5</option><option value="10">10</option><option value="15">15</option><option value="25">25</option></select></label>}</div>{hasFilter(selected, "comparison") && <label className="mt-3 flex items-center gap-2 text-xs font-bold"><input type="checkbox" name="comparison" defaultChecked={presetRequest?.comparison || false} /> مقارنة بالفترة السابقة</label>}</details>}

          <Button type="submit" disabled={pending} className="mt-4 w-full">{pending ? <LoaderCircle size={17} className="animate-spin" /> : <Play size={17} />} عرض التقرير</Button>
        </form>}
      </div>

      <div ref={resultRef}>{result && <ResultPanel key={`${result.analysis_key}-${result.period_label}`} result={result} />}</div>
      {state.error && <div className="mt-4 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{state.error}</div>}
    </section>
  );
}
