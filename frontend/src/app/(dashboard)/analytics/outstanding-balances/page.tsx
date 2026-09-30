import Link from "next/link";
import { ArrowRight, HandCoins } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";

type OutstandingBalances = {
  rows: Array<{
    patient_id: string;
    patient_name: string;
    phone: string | null;
    currency: string;
    balance_minor: number;
    appointment_count: number;
  }>;
  total_patients: number;
  totals_by_currency: Record<string, number>;
};

export default async function OutstandingBalancesReportPage() {
  const outstanding = await tiaRequest<OutstandingBalances>("/finance/outstanding-balances?limit=500");

  return (
    <>
      <PageHeader
        title="المبالغ المستحقة على العملاء"
        description="العملاء الذين أكملوا جلسة وما زال مبلغ مرتبط بالزيارة غير مسدد بالكامل."
        action={<Link href="/analytics" className="inline-flex items-center gap-2 rounded-xl border border-[var(--border)] bg-white px-3 py-2 text-sm font-bold"><ArrowRight size={16} />الرجوع للتقارير</Link>}
      />

      <div className="mb-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <Card>
          <CardContent className="pt-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-xs font-bold text-slate-500">عدد العملاء</div>
                <div className="mt-1 text-2xl font-black text-slate-950">{outstanding.total_patients.toLocaleString("ar-EG")}</div>
              </div>
              <span className="grid size-10 place-items-center rounded-xl bg-amber-50 text-amber-700"><HandCoins size={19} /></span>
            </div>
          </CardContent>
        </Card>
        {Object.entries(outstanding.totals_by_currency).map(([currency, amount]) => (
          <Card key={currency}>
            <CardContent className="pt-5">
              <div className="text-xs font-bold text-slate-500">إجمالي المتبقي · {currency}</div>
              <div className="mt-1 text-2xl font-black text-slate-950">{formatMoney(amount, currency)}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>الجلسات المكتملة غير المسددة بالكامل</CardTitle>
          <p className="mt-1 text-xs text-[var(--muted)]">الحجوزات المستقبلية والمواعيد المؤكدة قبل إتمام الجلسة لا تدخل. في جلسة الباكيدج لا يُحسب سعر الخدمة مرة ثانية، لكن أي منتجات أُضيفت للزيارة ولم تُدفع تظل مبلغًا مستحقًا.</p>
        </CardHeader>
        <CardContent>
          {outstanding.rows.length ? (
            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="data-table min-w-[680px]">
                <thead><tr><th>العميل</th><th>الهاتف</th><th>جلسات مكتملة غير مسددة</th><th>المتبقي</th><th></th></tr></thead>
                <tbody>
                  {outstanding.rows.map((row) => (
                    <tr key={`${row.patient_id}-${row.currency}`}>
                      <td className="font-black">{row.patient_name}</td>
                      <td dir="ltr">{row.phone || "—"}</td>
                      <td>{row.appointment_count.toLocaleString("ar-EG")}</td>
                      <td className="font-black">{formatMoney(row.balance_minor, row.currency)}</td>
                      <td><Link href={`/appointments?patient_id=${row.patient_id}&scope=all`} className="text-xs font-bold text-[var(--accent)] hover:underline">عرض المواعيد</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-[var(--muted)]">لا يوجد حاليًا عميل أكمل جلسة وما زال عليه مبلغ مرتبط بها.</div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
