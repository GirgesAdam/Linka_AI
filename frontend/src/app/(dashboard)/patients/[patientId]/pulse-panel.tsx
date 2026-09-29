import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { formatMoney } from "@/lib/format";
import { tiaRequest } from "@/lib/tia/api";
import type { PatientPulsePack, PulseBalance, PulsePackOffer } from "@/lib/types";
import { purchasePatientPulsePack, recordPatientPulsePackPayment } from "../actions";

function amountMajor(minor: number) {
  return (Math.max(0, minor) / 100).toFixed(2);
}

export async function PatientPulsePanel({ patientId }: { patientId: string }) {
  const [balances, packs, offers] = await Promise.all([
    tiaRequest<PulseBalance[]>(`/booking/patients/${patientId}/pulse-balance`),
    tiaRequest<PatientPulsePack[]>(`/booking/patients/${patientId}/pulse-packs`),
    tiaRequest<PulsePackOffer[]>("/booking/pulse-pack-offers?active_only=true"),
  ]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>رصيد الـPulses</CardTitle>
        <p className="text-xs leading-5 text-[var(--muted)]">
          الرصيد مرتبط بجهاز الليزر، ويُخصم فقط بعد تسجيل الاستهلاك الفعلي للجلسة.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {balances.length ? (
          <div className="grid gap-3 sm:grid-cols-2">
            {balances.map((balance) => (
              <div key={balance.device_key} className="rounded-xl border border-teal-100 bg-[var(--interactive-soft)]/50 p-4">
                <div className="text-xs font-bold text-[var(--interactive)]">{balance.device_name}</div>
                <div className="mt-1 text-2xl font-black text-slate-950">
                  {balance.pulses_remaining.toLocaleString("ar-EG")} <span className="text-sm">Pulse</span>
                </div>
                <div className="mt-1 text-xs text-[var(--muted)]">
                  {balance.pulses_consumed.toLocaleString("ar-EG")} مستخدمة · {balance.active_pack_count.toLocaleString("ar-EG")} باقة نشطة
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-xl bg-slate-50 p-4 text-sm text-[var(--muted)]">
            لا يوجد رصيد Pulses نشط لهذا العميل.
          </div>
        )}

        {packs.length > 0 && (
          <div className="space-y-2">
            <div className="text-sm font-black">تفاصيل الباقات</div>
            {packs.map((pack) => (
              <div key={pack.id} className="rounded-xl border border-[var(--border)] p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-bold">
                      {pack.pulses_purchased.toLocaleString("ar-EG")} Pulse · {pack.device_name}
                    </div>
                    <div className="mt-1 text-xs text-[var(--muted)]">
                      متبقي {pack.pulses_remaining.toLocaleString("ar-EG")} · مستخدم {pack.pulses_consumed.toLocaleString("ar-EG")}
                    </div>
                  </div>
                  <Badge tone={pack.effective_status === "active" ? "green" : "gray"}>
                    {pack.effective_status === "active" ? "نشطة" : pack.effective_status}
                  </Badge>
                </div>
                <div className="mt-2 text-xs text-slate-600">
                  {formatMoney(pack.amount_paid_minor, pack.currency)} مدفوع من {formatMoney(pack.sale_price_minor, pack.currency)}
                </div>
                {pack.effective_status === "active" && pack.balance_due_minor > 0 && (
                  <form action={recordPatientPulsePackPayment} className="mt-3 grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
                    <input type="hidden" name="patient_id" value={patientId} />
                    <input type="hidden" name="pack_id" value={pack.id} />
                    <Input name="amount" type="number" min="0.01" step="0.01" required
                      max={amountMajor(pack.balance_due_minor)} placeholder="المبلغ" />
                    <select name="payment_method" className="form-control h-10 min-h-10" defaultValue="cash">
                      <option value="cash">كاش</option>
                      <option value="visa">Visa</option>
                      <option value="instapay">InstaPay</option>
                    </select>
                    <Button type="submit" variant="outline">تسجيل دفعة</Button>
                  </form>
                )}
              </div>
            ))}
          </div>
        )}

        {offers.length > 0 && (
          <details className="rounded-xl border border-[var(--border)] p-3">
            <summary className="cursor-pointer text-sm font-black text-[var(--interactive)]">بيع باقة Pulses جديدة</summary>
            <form action={purchasePatientPulsePack} className="mt-3 grid gap-3 md:grid-cols-2">
              <input type="hidden" name="patient_id" value={patientId} />
              <label className="grid gap-1.5 text-xs font-bold text-slate-700 md:col-span-2">
                الباقة
                <select name="offer_id" required className="form-control h-10 min-h-10">
                  {offers.map((offer) => (
                    <option key={offer.id} value={offer.id}>
                      {offer.pulses_count.toLocaleString("ar-EG")} Pulse · {offer.device_name} · {formatMoney(offer.price_minor, offer.currency)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1.5 text-xs font-bold text-slate-700">
                المدفوع الآن
                <Input name="amount_paid" type="number" min="0" step="0.01" defaultValue="0" required />
              </label>
              <label className="grid gap-1.5 text-xs font-bold text-slate-700">
                طريقة الدفع
                <select name="payment_method" className="form-control h-10 min-h-10" defaultValue="cash">
                  <option value="cash">كاش</option>
                  <option value="visa">Visa</option>
                  <option value="instapay">InstaPay</option>
                </select>
              </label>
              <div className="md:col-span-2">
                <Button type="submit">إضافة الباقة للعميل</Button>
              </div>
            </form>
          </details>
        )}
      </CardContent>
    </Card>
  );
}
