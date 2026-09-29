"use client";

import { useState } from "react";

import { CircleDollarSign, Zap } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatMoney } from "@/lib/format";
import type { PulsePackOffer } from "@/lib/types";

import { recordAppointmentPayment } from "./actions";

function major(minor: number) {
  return (minor / 100).toFixed(2);
}

function parseMinor(value: string) {
  const normalized = value.trim().replace(",", ".");
  if (!/^\d+(?:\.\d{0,2})?$/.test(normalized)) return 0;
  const [whole, fraction = ""] = normalized.split(".");
  return Number(whole || "0") * 100 + Number((fraction + "00").slice(0, 2));
}

type BillingChoice = "standard" | "pulse_balance" | "pulse_pack" | "pulse_overage" | "pulse_pending";

type PulseCheckout = {
  lockedToPulse: boolean;
  deviceName: string;
  pulsesUsed: number;
  availableBalance: number;
  deficitPulses: number;
  overagePriceMinor: number | null;
  overageCurrency: string;
  offers: PulsePackOffer[];
  canSwitchToPulse: boolean;
};

export function AppointmentPaymentForm({
  appointmentId,
  patientId,
  currency,
  subtotalMinor,
  servicePriceMinor,
  discountMinor,
  netPaidMinor,
  balanceMinor,
  pulseCheckout,
}: {
  appointmentId: string;
  patientId: string;
  currency: string;
  subtotalMinor: number;
  servicePriceMinor: number;
  discountMinor: number;
  netPaidMinor: number;
  balanceMinor: number;
  pulseCheckout?: PulseCheckout | null;
}) {
  const [discount, setDiscount] = useState(major(discountMinor));
  const [amount, setAmount] = useState(major(balanceMinor));
  const [billingChoice, setBillingChoice] = useState<BillingChoice>(
    pulseCheckout?.lockedToPulse ? "pulse_pending" : "standard",
  );
  const [selectedOfferId, setSelectedOfferId] = useState("");

  function subtotalFor(choice: BillingChoice, offerId = selectedOfferId) {
    if (!pulseCheckout || choice === "standard" || choice === "pulse_pending") {
      return subtotalMinor;
    }
    const pulseBase = pulseCheckout.lockedToPulse
      ? subtotalMinor
      : Math.max(subtotalMinor - servicePriceMinor, 0);
    if (choice === "pulse_pack") {
      const offer = pulseCheckout.offers.find((item) => item.id === offerId);
      return pulseBase + (offer?.price_minor ?? 0);
    }
    if (choice === "pulse_overage") {
      return pulseBase + pulseCheckout.deficitPulses * (pulseCheckout.overagePriceMinor ?? 0);
    }
    return pulseBase;
  }

  function resetAmount(choice: BillingChoice, offerId = selectedOfferId, nextDiscount = discount) {
    const subtotal = subtotalFor(choice, offerId);
    const requestedDiscount = parseMinor(nextDiscount);
    const discountValue = Math.min(requestedDiscount, subtotal);
    if (requestedDiscount > subtotal) setDiscount(major(subtotal));
    const nextBalance = Math.max(subtotal - discountValue - netPaidMinor, 0);
    setAmount(major(nextBalance));
  }

  function updateDiscount(value: string) {
    setDiscount(value);
    resetAmount(billingChoice, selectedOfferId, value);
  }

  function chooseBilling(choice: BillingChoice) {
    setBillingChoice(choice);
    resetAmount(choice);
  }

  function chooseOffer(offerId: string) {
    setSelectedOfferId(offerId);
    if (billingChoice === "pulse_pack") resetAmount("pulse_pack", offerId);
  }

  const pulseMode =
    billingChoice === "pulse_balance"
      ? "use_balance"
      : billingChoice === "pulse_pack"
        ? "purchase_pack"
        : billingChoice === "pulse_overage"
          ? "overage"
          : "none";
  const pulseSelectionRequired =
    Boolean(pulseCheckout?.lockedToPulse) && billingChoice === "pulse_pending";
  const packSelectionRequired =
    billingChoice === "pulse_pack" && !selectedOfferId;
  const coveredFromBalance = pulseCheckout
    ? Math.max(pulseCheckout.pulsesUsed - pulseCheckout.deficitPulses, 0)
    : 0;

  return (
    <form action={recordAppointmentPayment} className="space-y-4 rounded-xl border border-[var(--border)] p-4">
      <input type="hidden" name="appointment_id" value={appointmentId} />
      <input type="hidden" name="patient_id" value={patientId} />
      <input type="hidden" name="pulse_mode" value={pulseMode} />
      <input type="hidden" name="pulse_pack_offer_id" value={billingChoice === "pulse_pack" ? selectedOfferId : ""} />

      {pulseCheckout && (
        <div className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-4">
          <div className="flex items-center gap-2 text-sm font-black text-slate-900">
            <Zap size={16} /> طريقة حساب جلسة الليزر
          </div>
          <div className="mt-2 grid gap-2 text-xs text-slate-900 sm:grid-cols-3">
            <div className="rounded-lg bg-white p-2">
              <span className="text-[var(--muted)]">الاستهلاك</span>
              <b className="mt-1 block">{pulseCheckout.pulsesUsed.toLocaleString("ar-EG")} Pulse</b>
            </div>
            <div className="rounded-lg bg-white p-2">
              <span className="text-[var(--muted)]">المتاح من الرصيد</span>
              <b className="mt-1 block">{pulseCheckout.availableBalance.toLocaleString("ar-EG")} Pulse</b>
            </div>
            <div className="rounded-lg bg-white p-2">
              <span className="text-[var(--muted)]">غير مغطى</span>
              <b className="mt-1 block">{pulseCheckout.deficitPulses.toLocaleString("ar-EG")} Pulse</b>
            </div>
          </div>

          {!pulseCheckout.lockedToPulse && (
            <label className="mt-3 flex cursor-pointer items-start gap-2 rounded-lg bg-white p-3 text-sm">
              <input
                type="radio"
                name="billing_choice"
                checked={billingChoice === "standard"}
                onChange={() => chooseBilling("standard")}
              />
              <span>
                <b className="block">سعر الجلسة العادي</b>
                <span className="text-xs text-[var(--muted)]">الخدمة الأساسية تفضل محسوبة بسعرها المعتاد.</span>
              </span>
            </label>
          )}

          {!pulseCheckout.canSwitchToPulse && !pulseCheckout.lockedToPulse ? (
            <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs font-semibold text-amber-900">
              فيه دفعة مسجلة بالفعل على سعر الجلسة العادي. اعمل الاسترداد أو التصحيح الأول قبل تحويل الجلسة إلى Pulses.
            </div>
          ) : (
            <div className="mt-3 grid gap-2">
              {pulseCheckout.deficitPulses === 0 && (
                <label className="flex cursor-pointer items-start gap-2 rounded-lg bg-white p-3 text-sm">
                  <input
                    type="radio"
                    name="billing_choice"
                    checked={billingChoice === "pulse_balance"}
                    onChange={() => chooseBilling("pulse_balance")}
                  />
                  <span>
                    <b className="block">الحساب من رصيد الـPulses</b>
                    <span className="text-xs text-[var(--muted)]">
                      يخصم {pulseCheckout.pulsesUsed.toLocaleString("ar-EG")} Pulse، وسعر الخدمة الأساسية لا يُحسب مرة ثانية.
                    </span>
                  </span>
                </label>
              )}

              {pulseCheckout.offers.length > 0 && (
                <div className="rounded-lg bg-white p-3">
                  <label className="flex cursor-pointer items-start gap-2 text-sm">
                    <input
                      type="radio"
                      name="billing_choice"
                      checked={billingChoice === "pulse_pack"}
                      onChange={() => chooseBilling("pulse_pack")}
                    />
                    <span>
                      <b className="block">شراء باقة Pulses</b>
                      <span className="text-xs text-[var(--muted)]">
                        يتم استخدام الرصيد القديم أولًا، ثم يُخصم المتبقي من الباقة الجديدة ويظل الباقي للجلسات القادمة.
                      </span>
                    </span>
                  </label>
                  {billingChoice === "pulse_pack" && (
                    <select
                      value={selectedOfferId}
                      onChange={(event) => chooseOffer(event.target.value)}
                      className="form-control mt-3 h-10 min-h-10"
                      required
                    >
                      <option value="">اختار الباقة</option>
                      {pulseCheckout.offers.map((offer) => (
                        <option key={offer.id} value={offer.id}>
                          {offer.pulses_count.toLocaleString("ar-EG")} Pulse · {formatMoney(offer.price_minor, offer.currency)}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              )}

              {pulseCheckout.deficitPulses > 0 && (
                <label className={"flex items-start gap-2 rounded-lg bg-white p-3 text-sm " + (pulseCheckout.overagePriceMinor ? "cursor-pointer" : "opacity-70")}>
                  <input
                    type="radio"
                    name="billing_choice"
                    checked={billingChoice === "pulse_overage"}
                    disabled={!pulseCheckout.overagePriceMinor}
                    onChange={() => chooseBilling("pulse_overage")}
                  />
                  <span>
                    <b className="block">دفع الـPulses غير المغطاة</b>
                    <span className="text-xs text-[var(--muted)]">
                      {coveredFromBalance > 0
                        ? "يُخصم " + coveredFromBalance.toLocaleString("ar-EG") + " Pulse من الرصيد أولًا، ثم "
                        : ""}
                      {pulseCheckout.overagePriceMinor
                        ? pulseCheckout.deficitPulses.toLocaleString("ar-EG") + " Pulse × " + formatMoney(pulseCheckout.overagePriceMinor, pulseCheckout.overageCurrency) + "."
                        : "سعر الـPulse الإضافية غير محدد لجهاز " + pulseCheckout.deviceName + "."}
                    </span>
                  </span>
                </label>
              )}
            </div>
          )}

          <div className="mt-3 text-[11px] leading-5 text-[var(--accent-strong)]">
            عند اختيار الحساب بالـPulses، سعر الخدمة الأساسية يُلغى من الحساب. المنتجات والخدمات الإضافية تظل محسوبة بشكل طبيعي.
          </div>
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-sm font-bold">
          الخصم ({currency})
          <input
            name="discount"
            inputMode="decimal"
            required
            value={discount}
            onChange={(event) => updateDiscount(event.target.value)}
            className="form-control mt-2 h-10 min-h-10"
          />
          <span className="mt-1 block text-[11px] font-medium text-[var(--muted)]">
            الخصم يقلل إجمالي الزيارة قبل تسجيل الدفعة.
          </span>
        </label>
        <label className="text-sm font-bold">
          المبلغ المحصل ({currency})
          <input
            name="amount"
            inputMode="decimal"
            required
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
            className="form-control mt-2 h-10 min-h-10"
          />
        </label>
        <label className="text-sm font-bold">
          طريقة الدفع
          <select name="payment_method" defaultValue="cash" className="form-control mt-2 h-10 min-h-10">
            <option value="cash">Cash</option>
            <option value="visa">Visa</option>
            <option value="instapay">InstaPay</option>
          </select>
        </label>
        <label className="text-sm font-bold">
          رقم الإيصال أو المرجع - اختياري
          <input name="external_reference" maxLength={128} placeholder="مثال: رقم الإيصال" className="form-control mt-2 h-10 min-h-10" />
        </label>
      </div>

      <Button disabled={pulseSelectionRequired || packSelectionRequired}>
        <CircleDollarSign size={15} />
        {parseMinor(amount) > 0 ? "تسجيل الحساب والدفعة" : "تأكيد الحساب"}
      </Button>
    </form>
  );
}
