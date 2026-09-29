"use client";

import { useMemo, useState } from "react";

import { Plus, Zap } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatMoney } from "@/lib/format";
import type { PulseBalance, PulseBillingSettings, PulsePackOffer } from "@/lib/types";

import { addAppointmentAdditionalService } from "./actions";
import type { AppointmentDevicePrice, AppointmentServiceOption } from "./service-editor";

type PulseResolution = "purchase_pack" | "overage";

export function AdditionalServiceForm({
  appointmentId,
  patientId,
  primaryServiceId,
  services,
  devicePrices,
  pulseBalances,
  pulsePackOffers,
  pulseSettings,
}: {
  appointmentId: string;
  patientId: string;
  primaryServiceId: string;
  services: AppointmentServiceOption[];
  devicePrices: AppointmentDevicePrice[];
  pulseBalances: PulseBalance[];
  pulsePackOffers: PulsePackOffer[];
  pulseSettings: PulseBillingSettings[];
}) {
  const [serviceId, setServiceId] = useState("");
  const [deviceKey, setDeviceKey] = useState("");
  const [billingMethod, setBillingMethod] = useState<"standard" | "pulse">("standard");
  const [pulsesUsed, setPulsesUsed] = useState("");
  const [resolution, setResolution] = useState<PulseResolution>("purchase_pack");
  const [offerId, setOfferId] = useState("");

  const availableServices = services.filter(
    (service) => service.is_active && service.id !== primaryServiceId,
  );
  const service = availableServices.find((item) => item.id === serviceId) ?? null;
  const devices = devicePrices.filter(
    (row) =>
      row.service_id === serviceId &&
      row.configured &&
      row.price_minor != null,
  );
  const selectedDevice = devices.find((row) => row.device_key === deviceKey) ?? null;
  const numericPulses = /^\d+$/.test(pulsesUsed) ? Number(pulsesUsed) : 0;
  const balance =
    pulseBalances.find((item) => item.device_key === deviceKey)?.pulses_remaining ?? 0;
  const deficit = Math.max(numericPulses - balance, 0);
  const setting =
    pulseSettings.find((item) => item.device_key === deviceKey) ?? null;
  const compatibleOffers = useMemo(
    () =>
      pulsePackOffers.filter(
        (offer) =>
          offer.device_key === deviceKey &&
          offer.pulses_count >= deficit,
      ),
    [pulsePackOffers, deviceKey, deficit],
  );

  const usePulse = billingMethod === "pulse";
  const pulseMode =
    !usePulse
      ? "none"
      : deficit === 0
        ? "use_balance"
        : resolution;
  const needsPack = usePulse && deficit > 0 && resolution === "purchase_pack";
  const validPulse =
    !usePulse ||
    (Boolean(service?.requires_laser_device) &&
      Boolean(selectedDevice) &&
      numericPulses > 0 &&
      (!needsPack || Boolean(offerId)) &&
      (deficit === 0 || resolution !== "overage" || Boolean(setting?.overage_price_minor)));
  const deviceReady = !service?.requires_laser_device || Boolean(selectedDevice);

  return (
    <form
      action={addAppointmentAdditionalService}
      className="space-y-3 rounded-xl border border-slate-200 bg-white p-3"
    >
      <input type="hidden" name="appointment_id" value={appointmentId} />
      <input type="hidden" name="patient_id" value={patientId} />
      <input type="hidden" name="pulse_mode" value={pulseMode} />
      <input
        type="hidden"
        name="pulse_pack_offer_id"
        value={needsPack ? offerId : ""}
      />

      <div className="grid gap-3 md:grid-cols-2">
        <label className="text-xs font-bold">
          الخدمة
          <select
            name="service_id"
            required
            value={serviceId}
            onChange={(event) => {
              setServiceId(event.target.value);
              setDeviceKey("");
              setBillingMethod("standard");
              setPulsesUsed("");
              setOfferId("");
            }}
            className="form-control mt-1.5 h-10 min-h-10"
          >
            <option value="" disabled>اختار خدمة إضافية</option>
            {availableServices.map((item) => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </select>
        </label>

        <label className="text-xs font-bold">
          جهاز الليزر - عند الحاجة
          <select
            name="laser_device_key"
            value={deviceKey}
            disabled={!service?.requires_laser_device}
            onChange={(event) => {
              setDeviceKey(event.target.value);
              setOfferId("");
            }}
            className="form-control mt-1.5 h-10 min-h-10"
            required={Boolean(service?.requires_laser_device)}
          >
            <option value="">
              {service?.requires_laser_device ? "اختار الجهاز" : "غير مطلوب"}
            </option>
            {devices.map((device) => (
              <option key={device.device_key} value={device.device_key}>
                {device.device_name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {service?.requires_laser_device && selectedDevice && (
        <div className="rounded-xl bg-slate-50 p-3">
          <div className="text-xs font-black text-slate-800">طريقة حساب الخدمة الإضافية</div>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <label className="flex cursor-pointer items-start gap-2 rounded-lg bg-white p-3 text-sm">
              <input
                type="radio"
                checked={billingMethod === "standard"}
                onChange={() => setBillingMethod("standard")}
              />
              <span>
                <b className="block">سعر الخدمة العادي</b>
                <span className="text-xs text-[var(--muted)]">
                  {formatMoney(selectedDevice.price_minor ?? 0, selectedDevice.currency)}
                </span>
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2 rounded-lg bg-white p-3 text-sm">
              <input
                type="radio"
                checked={billingMethod === "pulse"}
                onChange={() => setBillingMethod("pulse")}
              />
              <span>
                <b className="flex items-center gap-1"><Zap size={14} /> الحساب بالـPulses</b>
                <span className="text-xs text-[var(--muted)]">
                  المتاح: {balance.toLocaleString("ar-EG")} Pulse
                </span>
              </span>
            </label>
          </div>
        </div>
      )}

      {usePulse && service?.requires_laser_device && selectedDevice && (
        <div className="space-y-3 rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3">
          <label className="block text-xs font-bold">
            عدد الـPulses المستخدمة فعليًا
            <input
              name="pulses_used"
              type="number"
              min="1"
              step="1"
              required
              value={pulsesUsed}
              onChange={(event) => {
                setPulsesUsed(event.target.value);
                setOfferId("");
              }}
              className="form-control mt-1.5 h-10 min-h-10"
              placeholder="مثال: 450"
            />
          </label>

          {numericPulses > 0 && (
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <div className="rounded-lg bg-white p-2">
                <span className="text-[var(--muted)]">الرصيد الحالي</span>
                <b className="mt-1 block">{balance.toLocaleString("ar-EG")} Pulse</b>
              </div>
              <div className="rounded-lg bg-white p-2">
                <span className="text-[var(--muted)]">الاستهلاك</span>
                <b className="mt-1 block">{numericPulses.toLocaleString("ar-EG")} Pulse</b>
              </div>
              <div className="rounded-lg bg-white p-2">
                <span className="text-[var(--muted)]">العجز</span>
                <b className="mt-1 block">{deficit.toLocaleString("ar-EG")} Pulse</b>
              </div>
            </div>
          )}

          {numericPulses > 0 && deficit === 0 && (
            <div className="rounded-lg bg-white p-3 text-xs font-semibold text-slate-900">
              الرصيد يكفي. سيتم خصم الاستهلاك من Pulses العميل، ولن يضاف سعر الخدمة الإضافية للحساب.
            </div>
          )}

          {deficit > 0 && (
            <div className="space-y-2">
              <div className="text-xs font-black text-slate-900">تغطية العجز</div>
              <label className="flex cursor-pointer items-start gap-2 rounded-lg bg-white p-3 text-sm">
                <input
                  type="radio"
                  checked={resolution === "purchase_pack"}
                  onChange={() => setResolution("purchase_pack")}
                />
                <span>
                  <b className="block">شراء باقة Pulses</b>
                  <span className="text-xs text-[var(--muted)]">
                    الرصيد القديم يُستخدم أولًا والباقي يُخصم من الباقة الجديدة.
                  </span>
                </span>
              </label>
              {resolution === "purchase_pack" && (
                <select
                  value={offerId}
                  onChange={(event) => setOfferId(event.target.value)}
                  className="form-control h-10 min-h-10"
                  required
                >
                  <option value="">اختار باقة تغطي العجز</option>
                  {compatibleOffers.map((offer) => (
                    <option key={offer.id} value={offer.id}>
                      {offer.pulses_count.toLocaleString("ar-EG")} Pulse · {formatMoney(offer.price_minor, offer.currency)}
                    </option>
                  ))}
                </select>
              )}
              {!compatibleOffers.length && resolution === "purchase_pack" && (
                <div className="text-xs font-semibold text-amber-800">
                  مفيش باقة متاحة على الجهاز تغطي العجز الحالي.
                </div>
              )}

              <label className={"flex items-start gap-2 rounded-lg bg-white p-3 text-sm " + (setting?.overage_price_minor ? "cursor-pointer" : "opacity-70")}>
                <input
                  type="radio"
                  checked={resolution === "overage"}
                  disabled={!setting?.overage_price_minor}
                  onChange={() => setResolution("overage")}
                />
                <span>
                  <b className="block">دفع الـPulses الإضافية</b>
                  <span className="text-xs text-[var(--muted)]">
                    {setting?.overage_price_minor
                      ? deficit.toLocaleString("ar-EG") + " Pulse × " + formatMoney(setting.overage_price_minor, setting.currency)
                      : "سعر الـPulse الإضافية غير محدد للجهاز."}
                  </span>
                </span>
              </label>
            </div>
          )}
        </div>
      )}

      <Button disabled={!serviceId || !deviceReady || !validPulse}>
        <Plus size={14} /> إضافة للخدمة والحساب
      </Button>
      <div className="text-xs leading-5 text-[var(--muted)]">
        الخدمة الإضافية لا تغيّر وقت الموعد المحجوز. لو اخترت Pulses، سعر الخدمة نفسها لا يُحسب مرة ثانية.
      </div>
    </form>
  );
}
