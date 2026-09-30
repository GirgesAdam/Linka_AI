"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatMoney } from "@/lib/format";

import { cancelPatientPackage } from "../actions";

function major(minor: number) {
  return (Math.max(0, minor) / 100).toFixed(2);
}

function parseMinor(value: string) {
  const normalized = value.trim().replace(",", ".");
  if (!/^\d+(?:\.\d{0,2})?$/.test(normalized)) return 0;
  const [whole, fraction = ""] = normalized.split(".");
  return Number(whole || "0") * 100 + Number((fraction + "00").slice(0, 2));
}

export function PackageCancellationForm({
  patientId,
  packageId,
  currency,
  consumedSessions,
  defaultChargeMinor,
  amountPaidMinor,
  amountRefundedMinor,
}: {
  patientId: string;
  packageId: string;
  currency: string;
  consumedSessions: number;
  defaultChargeMinor: number;
  amountPaidMinor: number;
  amountRefundedMinor: number;
}) {
  const [target, setTarget] = useState(major(defaultChargeMinor));
  const netPaid = Math.max(amountPaidMinor - amountRefundedMinor, 0);
  const targetMinor = parseMinor(target);
  const difference = targetMinor - netPaid;

  return (
    <details className="mt-3 rounded-lg border border-red-200 bg-red-50/40 p-3">
      <summary className="flex min-h-10 cursor-pointer items-center text-xs font-black text-red-800">إلغاء الباكيدج وتسوية الحساب</summary>
      <form action={cancelPatientPackage} className="mt-3 space-y-3">
        <input type="hidden" name="patient_id" value={patientId} />
        <input type="hidden" name="package_id" value={packageId} />

        <div className="rounded-lg bg-white p-3 text-xs leading-5 text-slate-700">
          استخدم العميل <b>{consumedSessions}</b> جلسة. قيمة التسوية المقترحة محسوبة بسعر الجلسات الفردية وقت شراء الباكيدج.
        </div>

        <label className="block text-xs font-bold text-slate-700">
          المبلغ النهائي المطلوب احتسابه على العميل ({currency})
          <Input
            name="settlement_target"
            type="number"
            min="0"
            step="0.01"
            required
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            className="mt-1"
          />
          <span className="mt-1 block font-normal leading-5 text-[var(--muted)]">
            تقدر تعدل الرقم قبل الإلغاء لو محتاج تعمل تسوية مختلفة.
          </span>
        </label>

        <div className="rounded-lg border border-slate-200 bg-white p-3 text-xs">
          <div>صافي المسجل من العميل حاليًا: <b>{formatMoney(netPaid, currency)}</b></div>
          <div className="mt-1 font-black">
            {difference > 0
              ? `سيتم تحصيل ${formatMoney(difference, currency)} عند الإلغاء.`
              : difference < 0
                ? `سيتم رد ${formatMoney(Math.abs(difference), currency)} للعميل.`
                : "لا يوجد تحصيل أو استرداد إضافي."}
          </div>
        </div>

        {difference > 0 && (
          <label className="block text-xs font-bold text-slate-700">
            طريقة تحصيل الفرق
            <select name="payment_method" defaultValue="cash" className="form-control mt-1 h-10 min-h-10">
              <option value="cash">Cash</option>
              <option value="visa">Visa</option>
              <option value="instapay">InstaPay</option>
            </select>
          </label>
        )}
        {difference <= 0 && <input type="hidden" name="payment_method" value="cash" />}

        <Button type="submit" variant="danger">تأكيد إلغاء الباكيدج</Button>
      </form>
    </details>
  );
}
