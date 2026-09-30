"use client";

import { useMemo, useState } from "react";

import { formatMoney } from "@/lib/format";

type MethodRow = { payment_method: string; amount_minor: number };
type AppointmentStats = { completed: number; noShow: number; cancelled: number };
type RevenuePoint = { label: string; value: number };

type Props = {
  fullYear: boolean;
  startDate: string;
  endDate: string;
  revenuePoints: RevenuePoint[];
  newPatientLabels: string[];
  newPatientValues: Array<number | null>;
  paymentMethods: MethodRow[];
  appointments: AppointmentStats;
  currency: string;
};

const methodLabels: Record<string, string> = { cash: "Cash", visa: "Visa", instapay: "InstaPay", card: "بطاقة", bank_transfer: "تحويل بنكي", wallet: "محفظة", online: "Online", other: "أخرى", unknown: "غير محدد" };

function patientPeriodBuckets(fullYear: boolean, labels: string[], values: Array<number | null>) {
  if (fullYear) {
    const year = Number(labels.find((label) => /^\d{4}-\d{2}$/.test(label))?.slice(0, 4) || new Date().getFullYear());
    const totals = Array.from({ length: 12 }, () => 0);
    labels.forEach((label, index) => {
      const match = /^(\d{4})-(\d{2})$/.exec(label);
      if (match) totals[Number(match[2]) - 1] += values[index] ?? 0;
    });
    const monthNames = Array.from({ length: 12 }, (_, month) => new Intl.DateTimeFormat("ar-EG", { month: "short" }).format(new Date(Date.UTC(year, month, 1))));
    return monthNames.map((label, index) => ({ label, value: totals[index] }));
  }
  const totals = [0, 0, 0, 0];
  labels.forEach((label, index) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label);
    if (!match) return;
    const day = Number(match[3]);
    const bucket = day <= 7 ? 0 : day <= 14 ? 1 : day <= 21 ? 2 : 3;
    totals[bucket] += values[index] ?? 0;
  });
  return totals.map((value, index) => ({ label: `الأسبوع ${index + 1}`, value }));
}

function InteractiveLine({ data, currency }: { data: RevenuePoint[]; currency: string }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const width = 900, height = 280, padX = 45, padY = 32;
  const min = Math.min(0, ...data.map((item) => item.value));
  const max = Math.max(1, ...data.map((item) => item.value));
  const span = Math.max(1, max - min);
  const points = data.map((item, index) => ({
    ...item,
    x: data.length === 1 ? width / 2 : padX + index * ((width - padX * 2) / (data.length - 1)),
    y: height - padY - ((item.value - min) / span) * (height - padY * 2),
  }));
  return <div className="relative overflow-x-auto">
    <svg viewBox={`0 0 ${width} ${height}`} className="h-56 w-full sm:h-72 sm:min-w-[650px]" aria-label="تطور الدخل">
      {[0.25,0.5,0.75,1].map((ratio) => <line key={ratio} x1={padX} x2={width-padX} y1={height-padY-ratio*(height-padY*2)} y2={height-padY-ratio*(height-padY*2)} className="stroke-slate-100" />)}
      <polyline points={points.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" className="stroke-[var(--accent)]" strokeWidth="4" strokeLinejoin="round" strokeLinecap="round" />
      {points.map((point,index) => <g key={`${point.label}-${index}`} onMouseEnter={() => setHovered(index)} onMouseLeave={() => setHovered(null)} className="cursor-pointer">
        <circle cx={point.x} cy={point.y} r={hovered===index?7:5} className="fill-white stroke-[var(--accent)]" strokeWidth="3" />
        <circle cx={point.x} cy={point.y} r="18" fill="transparent" />
        <text x={point.x} y={height-8} textAnchor="middle" className="fill-slate-500 text-[11px]">{point.label}</text>
        {hovered===index && <g><rect x={Math.min(width-170, Math.max(5, point.x-80))} y={Math.max(4, point.y-62)} width="160" height="44" rx="10" className="fill-slate-950"/><text x={Math.min(width-90, Math.max(85, point.x))} y={Math.max(30, point.y-36)} textAnchor="middle" className="fill-white text-[12px] font-bold">{formatMoney(point.value, currency)}</text></g>}
      </g>)}
    </svg>
  </div>;
}

function HorizontalBars({ rows, money = false, currency = "EGP" }: { rows: Array<{label:string;value:number}>; money?: boolean; currency?: string }) {
  const max = Math.max(1, ...rows.map((row) => row.value));
  return <div className="space-y-3">{rows.map((row) => <div key={row.label} title={`${row.label}: ${money ? formatMoney(row.value,currency) : row.value.toLocaleString("ar-EG")}`}><div className="mb-1 flex items-center justify-between gap-3 text-xs"><span className="font-bold text-slate-700">{row.label}</span><span className="font-black text-slate-900">{money ? formatMoney(row.value,currency) : row.value.toLocaleString("ar-EG")}</span></div><div className="h-2.5 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-[var(--accent)] transition-all hover:opacity-75" style={{ width: `${Math.max(row.value > 0 ? 4 : 0, (row.value/max)*100)}%` }} /></div></div>)}</div>;
}

export function DashboardCharts(props: Props) {
  const patients = useMemo(() => patientPeriodBuckets(props.fullYear, props.newPatientLabels, props.newPatientValues), [props.fullYear, props.newPatientLabels, props.newPatientValues]);
  const methods = props.paymentMethods.filter((row) => row.amount_minor > 0).map((row) => ({ label: methodLabels[row.payment_method] || row.payment_method, value: row.amount_minor }));
  const outcomes = [
    { label: "جلسات مكتملة", value: props.appointments.completed },
    { label: "عدم حضور", value: props.appointments.noShow },
    { label: "إلغاءات", value: props.appointments.cancelled },
  ];
  return <div className="grid gap-5 xl:grid-cols-2">
    <div className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4 xl:col-span-2"><div className="mb-4"><h3 className="font-black text-slate-950">تطور صافي الدخل</h3><p className="mt-1 text-xs text-slate-500">{props.startDate} → {props.endDate} · {props.fullYear ? "12 نقطة شهرية" : "4 نقاط أسبوعية"}</p></div><InteractiveLine data={props.revenuePoints} currency={props.currency} /></div>
    <div className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4"><h3 className="mb-4 font-black text-slate-950">طرق التحصيل</h3>{methods.length ? <HorizontalBars rows={methods} money currency={props.currency} /> : <p className="text-sm text-slate-500">لا توجد مدفوعات في الفترة.</p>}</div>
    <div className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4"><h3 className="mb-4 font-black text-slate-950">نتائج المواعيد</h3><HorizontalBars rows={outcomes} /></div>
    <div className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4 xl:col-span-2"><h3 className="mb-4 font-black text-slate-950">العملاء الجدد خلال الفترة</h3><HorizontalBars rows={patients} /></div>
  </div>;
}
