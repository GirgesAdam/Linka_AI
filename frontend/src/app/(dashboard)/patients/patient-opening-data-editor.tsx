"use client";

import { Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice, Service, ServicePackageOffer } from "@/lib/types";
import { getActiveOffersForService } from "./registration-utils";
import { loadPatientOpeningDataAction, savePatientOpeningDataAction } from "./actions";

const CURRENT_PACKAGE = "__current_opening_package__";

type OpeningData = {
  patient_id: string;
  customer_id: string | null;
  package: {
    package_id: string;
    offer_id: string | null;
    service_id: string;
    name: string;
    sessions_total: number;
    sessions_remaining: number;
    current_remaining: number;
    sessions_reserved: number;
    sessions_consumed: number;
    replaceable_identity: boolean;
  } | null;
  pulse_balances: Array<{
    opening_pack_id: string;
    device_key: string;
    device_name: string;
    opening_pulses: number;
    current_remaining: number;
    pulses_consumed: number;
    replaceable_identity: boolean;
  }>;
};

type PulseRow = {
  id: number;
  deviceKey: string;
  deviceName: string;
  pulsesRemaining: string;
  currentRemaining: number | null;
  replaceableIdentity: boolean;
};

export function PatientOpeningDataEditor({
  patientId,
  offers,
  devices,
  services,
}: {
  patientId: string;
  offers: ServicePackageOffer[];
  devices: ClinicLaserDevice[];
  services: Service[];
}) {
  const nextRowId = useRef(1);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<OpeningData | null>(null);
  const [customerId, setCustomerId] = useState("");
  const [selectedServiceId, setSelectedServiceId] = useState("");
  const [selectedOfferId, setSelectedOfferId] = useState("");
  const [sessionsRemaining, setSessionsRemaining] = useState("");
  const [pulseRows, setPulseRows] = useState<PulseRow[]>([]);

  function applyData(data: OpeningData) {
    setLoaded(data);
    setCustomerId(data.customer_id || "");
    if (data.package) {
      setSelectedServiceId(data.package.service_id);
      const activeCurrent = data.package.offer_id
        ? offers.some((offer) => offer.id === data.package?.offer_id && offer.is_active)
        : false;
      setSelectedOfferId(activeCurrent ? data.package.offer_id || "" : CURRENT_PACKAGE);
      setSessionsRemaining(String(data.package.sessions_remaining));
    } else {
      setSelectedServiceId("");
      setSelectedOfferId("");
      setSessionsRemaining("");
    }
    nextRowId.current = data.pulse_balances.length + 1;
    setPulseRows(
      data.pulse_balances.map((balance, index) => ({
        id: index + 1,
        deviceKey: balance.device_key,
        deviceName: balance.device_name,
        pulsesRemaining: String(balance.opening_pulses),
        currentRemaining: balance.current_remaining,
        replaceableIdentity: balance.replaceable_identity,
      })),
    );
  }

  async function openEditor() {
    setExpanded(true);
    setError(null);
    setSuccess(null);
    if (loaded) return;
    setLoading(true);
    const result = await loadPatientOpeningDataAction(patientId);
    setLoading(false);
    if (!result.data) {
      setError(result.error || "تعذر تحميل بيانات العميل.");
      return;
    }
    applyData(result.data as OpeningData);
  }

  const currentPackage = loaded?.package || null;
  const packageIdentityLocked = Boolean(currentPackage && !currentPackage.replaceable_identity);
  const serviceOffers = getActiveOffersForService(offers, selectedServiceId);
  const selectedOffer = serviceOffers.find((offer) => offer.id === selectedOfferId);
  const maxSessions =
    selectedOffer?.sessions_count ||
    (currentPackage && selectedOfferId === CURRENT_PACKAGE ? currentPackage.sessions_total : null);
  const activeServices = services.filter((service) => service.is_active);
  const selectedDeviceKeys = pulseRows.map((row) => row.deviceKey).filter(Boolean);

  async function save() {
    if (!loaded) return;
    setError(null);
    setSuccess(null);

    let packagePayload: { offer_id: string | null; sessions_remaining: number } | null = null;
    const hasPackage = Boolean(currentPackage || selectedOfferId);
    if (hasPackage) {
      const remaining = Number(sessionsRemaining);
      if (!Number.isInteger(remaining) || remaining <= 0) {
        setError("أدخل عدد الجلسات المتبقية بشكل صحيح.");
        return;
      }
      if (!maxSessions || remaining > maxSessions) {
        setError("عدد الجلسات المتبقية لا يمكن أن يتجاوز إجمالي جلسات الباكدج.");
        return;
      }
      if (!selectedOfferId) {
        setError("اختر عرض الباكدج قبل الحفظ.");
        return;
      }
      packagePayload = {
        offer_id: selectedOfferId === CURRENT_PACKAGE ? null : selectedOfferId,
        sessions_remaining: remaining,
      };
    }

    const seen = new Set<string>();
    const pulsePayload: Array<{ device_key: string; pulses_remaining: number }> = [];
    for (const row of pulseRows) {
      const pulses = Number(row.pulsesRemaining);
      if (!row.deviceKey || !Number.isInteger(pulses) || pulses <= 0 || pulses > 1_000_000_000) {
        setError("راجع جهاز الليزر ورصيد الـPulses لكل صف.");
        return;
      }
      const key = row.deviceKey.toLocaleLowerCase();
      if (seen.has(key)) {
        setError("لا يمكن تكرار نفس الجهاز في أرصدة Pulse.");
        return;
      }
      seen.add(key);
      pulsePayload.push({ device_key: row.deviceKey, pulses_remaining: pulses });
    }

    setSaving(true);
    const result = await savePatientOpeningDataAction(patientId, {
      customer_id: customerId.trim() || null,
      package: packagePayload,
      pulse_balances: pulsePayload,
    });
    setSaving(false);
    if (!result.data) {
      setError(result.error || "تعذر تحديث بيانات العميل.");
      return;
    }
    applyData(result.data as OpeningData);
    setSuccess("تم تحديث بيانات العميل.");
  }

  return (
    <div className="rounded-xl border border-[var(--border)] bg-white p-3">
      <Button type="button" onClick={openEditor} disabled={loading}>
        {loading ? "جارٍ تحميل البيانات..." : "استكمال / تحديث بيانات العميل"}
      </Button>

      {expanded && (
        <div className="mt-4 space-y-4 border-t border-[var(--border)] pt-4">
          {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm font-medium text-red-800">{error}</p>}
          {success && <p className="rounded-lg bg-emerald-50 p-3 text-sm font-bold text-emerald-800">{success}</p>}

          {loaded && (
            <>
              <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                Customer ID
                <Input value={customerId} onChange={(event) => setCustomerId(event.target.value)} dir="ltr" />
              </label>

              <details open={Boolean(currentPackage)} className="rounded-xl border border-[var(--border)] p-3">
                <summary className="cursor-pointer text-sm font-bold text-[var(--interactive)]">الرصيد الافتتاحي للباكدج</summary>
                <div className="mt-3 space-y-3">
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    الخدمة
                    <select
                      value={selectedServiceId}
                      disabled={packageIdentityLocked}
                      onChange={(event) => {
                        setSelectedServiceId(event.target.value);
                        setSelectedOfferId("");
                        setSessionsRemaining("");
                      }}
                      className="form-control h-10 min-h-10"
                    >
                      <option value="">اختر الخدمة</option>
                      {currentPackage && !activeServices.some((service) => service.id === currentPackage.service_id) && (
                        <option value={currentPackage.service_id}>{currentPackage.name} (الحالية)</option>
                      )}
                      {activeServices.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    الباكدج
                    <select
                      value={selectedOfferId}
                      disabled={packageIdentityLocked || !selectedServiceId}
                      onChange={(event) => {
                        setSelectedOfferId(event.target.value);
                        setSessionsRemaining("");
                      }}
                      className="form-control h-10 min-h-10"
                    >
                      {!currentPackage && <option value="">بدون رصيد باكدج</option>}
                      {currentPackage && selectedServiceId === currentPackage.service_id &&
                        (selectedOfferId === CURRENT_PACKAGE || !currentPackage.offer_id) && (
                          <option value={CURRENT_PACKAGE}>{currentPackage.name} · {currentPackage.sessions_total} جلسة (الحالية)</option>
                        )}
                      {serviceOffers.map((offer) => (
                        <option key={offer.id} value={offer.id}>
                          {offer.device_name ? `${offer.device_name} · ` : ""}{offer.sessions_count} جلسة
                        </option>
                      ))}
                    </select>
                  </label>
                  {(currentPackage || selectedOfferId) && (
                    <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                      الجلسات المتبقية الافتتاحية
                      <Input
                        type="number"
                        min={1}
                        max={maxSessions || undefined}
                        step={1}
                        value={sessionsRemaining}
                        onChange={(event) => setSessionsRemaining(event.target.value)}
                      />
                      {currentPackage && (
                        <span className="text-xs font-normal text-[var(--muted)]">
                          المتاح حاليًا بعد الاستخدام: {currentPackage.current_remaining}
                        </span>
                      )}
                    </label>
                  )}
                  {packageIdentityLocked && (
                    <p className="text-xs text-[var(--muted)]">يمكن تعديل الرصيد الافتتاحي، لكن لا يمكن تغيير هوية الباكدج لوجود تاريخ مسجل عليها.</p>
                  )}
                </div>
              </details>

              <details open={pulseRows.length > 0} className="rounded-xl border border-[var(--border)] p-3">
                <summary className="cursor-pointer text-sm font-bold text-[var(--interactive)]">أرصدة Pulse الافتتاحية</summary>
                <div className="mt-3 space-y-3">
                  {pulseRows.map((row, index) => {
                    const activeRowDevice = devices.find((device) => device.device_key === row.deviceKey);
                    const available = devices.filter(
                      (device) => device.device_key === row.deviceKey || !selectedDeviceKeys.includes(device.device_key),
                    );
                    return (
                      <div key={row.id} className="grid gap-3 rounded-lg bg-[var(--surface-2)] p-3 sm:grid-cols-[1fr_1fr_auto]">
                        <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                          الجهاز {index + 1}
                          <select
                            value={row.deviceKey}
                            disabled={!row.replaceableIdentity}
                            onChange={(event) => setPulseRows((current) => current.map((item) =>
                              item.id === row.id ? { ...item, deviceKey: event.target.value, deviceName: "", currentRemaining: null } : item,
                            ))}
                            className="form-control h-10 min-h-10"
                          >
                            {!activeRowDevice && row.deviceKey && <option value={row.deviceKey}>{row.deviceName} (الحالي)</option>}
                            {available.map((device) => <option key={device.id} value={device.device_key}>{device.name}</option>)}
                          </select>
                        </label>
                        <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                          النبضات المتبقية الافتتاحية
                          <Input
                            type="number"
                            min={1}
                            max={1_000_000_000}
                            step={1}
                            value={row.pulsesRemaining}
                            onChange={(event) => setPulseRows((current) => current.map((item) =>
                              item.id === row.id ? { ...item, pulsesRemaining: event.target.value } : item,
                            ))}
                          />
                          {row.currentRemaining !== null && (
                            <span className="text-xs font-normal text-[var(--muted)]">المتاح حاليًا بعد الاستخدام: {row.currentRemaining}</span>
                          )}
                        </label>
                        {row.replaceableIdentity && (
                          <button
                            type="button"
                            aria-label={`حذف رصيد الجهاز ${index + 1}`}
                            onClick={() => setPulseRows((current) => current.filter((item) => item.id !== row.id))}
                            className="self-end rounded-lg p-2 text-slate-500 hover:bg-white hover:text-red-700"
                          >
                            <Trash2 size={17} />
                          </button>
                        )}
                      </div>
                    );
                  })}
                  <Button
                    type="button"
                    disabled={pulseRows.length >= devices.length}
                    onClick={() => {
                      const device = devices.find((item) => !selectedDeviceKeys.includes(item.device_key));
                      if (!device) return;
                      const id = nextRowId.current++;
                      setPulseRows((current) => [...current, {
                        id,
                        deviceKey: device.device_key,
                        deviceName: device.name,
                        pulsesRemaining: "",
                        currentRemaining: null,
                        replaceableIdentity: true,
                      }]);
                    }}
                  >
                    <Plus size={16} />
                    إضافة جهاز
                  </Button>
                </div>
              </details>

              <div className="flex justify-end">
                <Button type="button" onClick={save} disabled={saving}>
                  {saving ? "جارٍ حفظ البيانات..." : "حفظ البيانات"}
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
