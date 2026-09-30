import type { LucideIcon } from "lucide-react";
import { CalendarCheck2, CircleDollarSign, ReceiptText, TrendingUp, UsersRound } from "lucide-react";

import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";
import type { AnalyticsCatalogRun, AnalyticsCatalogRunRequest } from "@/lib/types";
import { DashboardCharts } from "./dashboard-charts";

type Profitability = {
  start_date: string;
  end_date: string;
  currencies: Array<{
    currency: string;
    gross_payments_minor: number;
    refunds_minor: number;
    net_revenue_minor: number;
    expenses_minor: number;
    profit_minor: number;
  }>;
};

type FinanceTrend = {
  currency: string;
  points: Array<{
    label: string;
    start_date: string;
    end_date: string;
    net_revenue_minor: number;
  }>;
};

type PaymentBreakdown = {
  rows: Array<{ payment_method: string; currency: string; amount_minor: number; transaction_count: number }>;
};

function requestFor(analysisKey: string, startDate: string, endDate: string, granularity: "day" | "month" | null = null): AnalyticsCatalogRunRequest {
  return {
    analysis_key: analysisKey,
    lookback_days: null,
    all_history: false,
    start_date: startDate,
    end_date: endDate,
    service_ids: [],
    branch_ids: [],
    doctor_ids: [],
    comparison: false,
    granularity,
    limit: null,
    inactivity_days: null,
    min_visits: null,
    max_visits: null,
    has_future_appointment: null,
    marketing_consent: null,
  };
}

function metricNumber(result: AnalyticsCatalogRun, key: string) {
  const metric = result.rows[0]?.metrics.find((item) => item.key === key);
  return typeof metric?.value === "number" ? metric.value : 0;
}

function OverviewMetric({
  label,
  value,
  detail,
  icon: Icon,
  emphasis = false,
}: {
  label: string;
  value: string | number;
  detail: string;
  icon: LucideIcon;
  emphasis?: boolean;
}) {
  return (
    <div className={`rounded-2xl border p-4 shadow-sm ${emphasis ? "border-[var(--accent-border)] bg-[var(--accent-soft)]" : "border-[var(--border)] bg-white"}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-bold text-[var(--muted)]">{label}</div>
          <div className="mt-2 text-2xl font-black tracking-tight text-[var(--text)]">{value}</div>
          <div className="mt-1 text-[11px] leading-5 text-[var(--muted)]">{detail}</div>
        </div>
        <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-white text-[var(--accent)] shadow-sm">
          <Icon size={18} />
        </span>
      </div>
    </div>
  );
}

export async function AnalyticsOverviewPanel({
  startDate,
  endDate,
  periodLabel,
  fullYear,
}: {
  startDate: string;
  endDate: string;
  periodLabel: string;
  fullYear: boolean;
}) {
  const granularity = fullYear ? "month" : "day";
  const trendMode = fullYear ? "year" : "month";
  const [appointments, newPatients, trend, profitability, paymentBreakdown] = await Promise.all([
    tiaRequest<AnalyticsCatalogRun>("/analytics/catalog/run", {
      method: "POST",
      body: JSON.stringify(requestFor("appointment_overview", startDate, endDate)),
    }),
    tiaRequest<AnalyticsCatalogRun>("/analytics/catalog/run", {
      method: "POST",
      body: JSON.stringify(requestFor("new_patients_trend", startDate, endDate, granularity)),
    }),
    tiaRequest<FinanceTrend>(`/finance/dashboard-trend?start_date=${startDate}&end_date=${endDate}&mode=${trendMode}`),
    tiaRequest<Profitability>(`/finance/profitability?start_date=${startDate}&end_date=${endDate}`),
    tiaRequest<PaymentBreakdown>(`/finance/payment-method-breakdown?start_date=${startDate}&end_date=${endDate}`),
  ]);

  const finance = profitability.currencies.find((item) => item.currency === "EGP") || profitability.currencies[0];
  const currency = finance?.currency || trend.currency || "EGP";
  const newPatientSeries = newPatients.chart_data.series.find((item) => item.key === "new_patients") || newPatients.chart_data.series[0];
  const newPatientCount = newPatientSeries?.values.reduce<number>((sum, value) => sum + (value ?? 0), 0) ?? 0;
  const paymentMethods = paymentBreakdown.rows.filter((row) => row.currency === currency);
  const totalAppointments = metricNumber(appointments, "appointments");
  const completedAppointments = metricNumber(appointments, "completed_appointments");

  return (
    <section className="mb-8">
      <div className="mb-4">
        <h2 className="text-lg font-black text-slate-950">ملخص {periodLabel}</h2>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
          ملخص مالي وتشغيلي للفترة من {startDate} إلى {endDate} مبني على البيانات المسجلة فعليًا.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <OverviewMetric
          label="صافي الدخل"
          value={finance ? formatMoney(finance.net_revenue_minor, currency) : "—"}
          detail="المقبوضات بعد خصم المرتجعات المسجلة"
          icon={TrendingUp}
          emphasis
        />
        <OverviewMetric
          label="الربح المسجل"
          value={finance ? formatMoney(finance.profit_minor, currency) : "—"}
          detail="صافي الدخل بعد المصروفات المسجلة"
          icon={CircleDollarSign}
        />
        <OverviewMetric
          label="إجمالي المواعيد"
          value={totalAppointments.toLocaleString("ar-EG")}
          detail={`${completedAppointments.toLocaleString("ar-EG")} جلسة مكتملة خلال الفترة`}
          icon={CalendarCheck2}
        />
        <OverviewMetric
          label="عملاء جدد"
          value={newPatientCount.toLocaleString("ar-EG")}
          detail="عملاء أضيفوا خلال الفترة وفق عقد التقرير الحالي"
          icon={UsersRound}
        />
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div className="flex items-center justify-between gap-4 rounded-2xl border border-[var(--border)] bg-white px-4 py-3">
          <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><CircleDollarSign size={16} /> إجمالي المقبوضات</div>
          <div className="text-sm font-black text-[var(--text)]">{finance ? formatMoney(finance.gross_payments_minor, currency) : "—"}</div>
        </div>
        <div className="flex items-center justify-between gap-4 rounded-2xl border border-[var(--border)] bg-white px-4 py-3">
          <div className="flex items-center gap-2 text-xs font-bold text-[var(--muted)]"><ReceiptText size={16} /> المصروفات المسجلة</div>
          <div className="text-sm font-black text-[var(--text)]">{finance ? formatMoney(finance.expenses_minor, currency) : "—"}</div>
        </div>
      </div>

      <div className="mt-5">
        <DashboardCharts
          fullYear={fullYear}
          startDate={startDate}
          endDate={endDate}
          revenuePoints={trend.points.map((point) => ({ label: point.label, value: point.net_revenue_minor }))}
          newPatientLabels={newPatients.chart_data.labels}
          newPatientValues={newPatientSeries?.values || []}
          paymentMethods={paymentMethods.map((row) => ({ payment_method: row.payment_method, amount_minor: row.amount_minor }))}
          appointments={{
            completed: completedAppointments,
            noShow: metricNumber(appointments, "no_show_appointments"),
            cancelled: metricNumber(appointments, "cancelled_appointments"),
          }}
          currency={currency}
        />
      </div>
    </section>
  );
}
