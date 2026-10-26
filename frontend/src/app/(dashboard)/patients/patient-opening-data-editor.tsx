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
  packages: Array<{
    package_id: string;
    offer_id: string | null;
    service_id: string;
    name: string;
    sessions_total: number;
    opening_sessions_remaining: number;
    current_remaining: number;
    sessions_reserved: number;
    sessions_consumed: number;
    replaceable_identity: boolean;
  }>;
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

type PackageRow = {
  id: number;
  packageId: string | null;
  serviceId: string;
  selectedOfferId: string;
  name: string;
  sessionsTotal: number | null;
  sessionsRemaining: string;
  currentRemaining: number | null;
  replaceableIdentity: boolean;
};

type PulseRow = {
  id: number;
  deviceKey: string;
  deviceName: string;
  pulsesRemaining: string;
  currentRemaining: number | null;
  replaceableIdentity: boolean;
};

function emptyPackageRow(id: number): PackageRow {
  return {
    id,
    packageId: null,
    serviceId: "",
    selectedOfferId: "",
    name: "",
    sessionsTotal: null,
    sessionsRemaining: "",
    currentRemaining: null,
    replaceableIdentity: true,
  };
}

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
  const nextPackageRowId = useRef(1);
  const nextPulseRowId = useRef(1);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<OpeningData | null>(null);
  const [customerId, setCustomerId] = useState("");
  const [packageRows, setPackageRows] = useState<PackageRow[]>([]);
  const [pulseRows, setPulseRows] = useState<PulseRow[]>([]);

  function applyData(data: OpeningData) {
    setLoaded(data);
    setCustomerId(data.customer_id || "");
    nextPackageRowId.current = data.packages.length + 1;
    setPackageRows(
      data.packages.map((pkg, index) => {
        const activeCurrent = pkg.offer_id
          ? offers.some((offer) => offer.id === pkg.offer_id && offer.is_active)
          : false;
        return {
          id: index + 1,
          packageId: pkg.package_id,
          serviceId: pkg.service_id,
          selectedOfferId: activeCurrent ? pkg.offer_id || "" : CURRENT_PACKAGE,
          name: pkg.name,
          sessionsTotal: pkg.sessions_total,
          sessionsRemaining: String(pkg.opening_sessions_remaining),
          currentRemaining: pkg.current_remaining,
          replaceableIdentity: pkg.replaceable_identity,
        };
      }),
    );
    nextPulseRowId.current = data.pulse_balances.length + 1;
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

  const activeServices = services.filter((service) => service.is_active);
  const selectedOfferIds = packageRows
    .map((row) => row.selectedOfferId)
    .filter((offerId) => offerId && offerId !== CURRENT_PACKAGE);
  const selectedDeviceKeys = pulseRows.map((row) => row.deviceKey).filter(Boolean);

  async function save() {
    if (!loaded) return;
    setError(null);
    setSuccess(null);

    const packagePayload: Array<{
      package_id: string | null;
      offer_id: string | null;
      sessions_remaining: number;
    }> = [];
    const seenOffers = new Set<string>();
    for (const row of packageRows) {
      const completelyBlank = !row.packageId && !row.serviceId && !row.selectedOfferId && !row.sessionsRemaining;
      if (completelyBlank) continue;
      const serviceOffers = getActiveOffersForService(offers, row.serviceId);
      const selectedOffer = serviceOffers.find((offer) => offer.id === row.selectedOfferId);
      const maxSessions = selectedOffer?.sessions_count || (row.selectedOfferId === CURRENT_PACKAGE ? row.sessionsTotal : null);
      const remaining = Number(row.sessionsRemaining);
      if (!row.serviceId || !row.selectedOfferId) {
        setError("اختر الخدمة والباكيدج لكل رصيد افتتاحي.");
        return;
      }
      if (!Number.isInteger(remaining) || remaining <= 0 || !maxSessions || remaining > maxSessions) {
        setError("راجع عدد الجلسات المتبقية لكل باكيدج.");
        return;
      }
      if (row.selectedOfferId !== CURRENT_PACKAGE) {
        if (seenOffers.has(row.selectedOfferId)) {
          setError("لا يمكن تكرار نفس الباكيدج في الأرصدة الافتتاحية.");
          return;
        }
        seenOffers.add(row.selectedOfferId);
      }
      packagePayload.push({
        package_id: row.packageId,
        offer_id: row.selectedOfferId === CURRENT_PACKAGE ? null : row.selectedOfferId,
        sessions_remaining: remaining,
      });
    }

    const seenDevices = new Set<string>();
    const pulsePayload: Array<{ device_key: string; pulses_remaining: number }> = [];
    for (const row of pulseRows) {
      const pulses = Number(row.pulsesRemaining);
      if (!row.deviceKey || !Number.isInteger(pulses) || pulses <= 0 || pulses > 1_000_000_000) {
        setError("راجع جهاز الليزر ورصيد الـPulses لكل صف.");
        return;
      }
      const key = row.deviceKey.toLocaleLowerCase();
      if (seenDevices.has(key)) {
        setError("لا يمكن تكرار نفس الجهاز في أرصدة Pulse.");
        return;
      }
      seenDevices.add(key);
      pulsePayload.push({ device_key: row.deviceKey, pulses_remaining: pulses });
    }

    setSaving(true);
    const result = await savePatientOpeningDataAction(patientId, {
      customer_id: customerId.trim() || null,
      packages: packagePayload,
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
                ID
                <Input value={customerId} onChange={(event) => setCustomerId(event.target.value)} dir="ltr" />
              </label>

              <details open={packageRows.length > 0} className="rounded-xl border border-[var(--border)] p-3">
                <summary className="cursor-pointer text-sm font-bold text-[var(--interactive)]">أرصدة الباكيدجات الافتتاحية</summary>
                <div className="mt-3 space-y-3">
                  {packageRows.map((row, index) => {
                    const identityLocked = Boolean(row.packageId && !row.replaceableIdentity);
                    const serviceOffers = getActiveOffersForService(offers, row.serviceId);
                    const availableOffers = serviceOffers.filter(
                      (offer) => offer.id === row.selectedOfferId || !selectedOfferIds.includes(offer.id),
                    );
                    const selectedOffer = serviceOffers.find((offer) => offer.id === row.selectedOfferId);
                    const maxSessions = selectedOffer?.sessions_count || (row.selectedOfferId === CURRENT_PACKAGE ? row.sessionsTotal : null);
                    return (
                      <div key={row.id} className="space-y-3 rounded-lg bg-[var(--surface-2)] p-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="text-sm font-black">باكيدج {index + 1}</div>
                          {row.replaceableIdentity && (
                            <button
                              type="button"
                              aria-label={`حذف الباكيدج ${index + 1}`}
                              onClick={() => setPackageRows((current) => current.filter((item) => item.id !== row.id))}
                              className="rounded-lg p-2 text-slate-500 hover:bg-white hover:text-red-700"
                            >
                              <Trash2 size={17} />
                            </button>
                          )}
                        </div>
                        <div className="grid gap-3 sm:grid-cols-2">
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            الخدمة
                            <select
                              value={row.serviceId}
                              disabled={identityLocked}
                              onChange={(event) => setPackageRows((current) => current.map((item) =>
                                item.id === row.id
                                  ? { ...item, serviceId: event.target.value, selectedOfferId: "", sessionsRemaining: "" }
                                  : item,
                              ))}
                              className="form-control h-10 min-h-10"
                            >
                              <option value="">اختر الخدمة</option>
                              {row.packageId && !activeServices.some((service) => service.id === row.serviceId) && (
                                <option value={row.serviceId}>{row.name} (الحالية)</option>
                              )}
                              {activeServices.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}
                            </select>
                          </label>
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            الباكيدج
                            <select
                              value={row.selectedOfferId}
                              disabled={identityLocked || !row.serviceId}
                              onChange={(event) => setPackageRows((current) => current.map((item) =>
                                item.id === row.id ? { ...item, selectedOfferId: event.target.value, sessionsRemaining: "" } : item,
                              ))}
                              className="form-control h-10 min-h-10"
                            >
                              <option value="">اختر الباكيدج</option>
                              {row.packageId && row.selectedOfferId === CURRENT_PACKAGE && (
                                <option value={CURRENT_PACKAGE}>{row.name} · {row.sessionsTotal} جلسة (الحالية)</option>
                              )}
                              {availableOffers.map((offer) => (
                                <option key={offer.id} value={offer.id}>
                                  {offer.device_name ? `${offer.device_name} · ` : ""}{offer.sessions_count} جلسة
                                </option>
                              ))}
                            </select>
                          </label>
                        </div>
                        <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                          الجلسات المتبقية الافتتاحية
                          <Input
                            type="number"
                            min={1}
                            max={maxSessions || undefined}
                            step={1}
                            value={row.sessionsRemaining}
                            disabled={!row.selectedOfferId}
                            onChange={(event) => setPackageRows((current) => current.map((item) =>
                              item.id === row.id ? { ...item, sessionsRemaining: event.target.value } : item,
                            ))}
                          />
                          {row.currentRemaining !== null && (
                            <span className="text-xs font-normal text-[var(--muted)]">المتاح حاليًا بعد الاستخدام: {row.currentRemaining}</span>
                          )}
                        </label>
                        {identityLocked && (
                          <p className="text-xs text-[var(--muted)]">يمكن تعديل الرصيد الافتتاحي، لكن لا يمكن تغيير أو حذف هذه الباكيدج لوجود تاريخ مسجل عليها.</p>
                        )}
                      </div>
                    );
                  })}
                  <Button
                    type="button"
                    variant="outline"
                    disabled={packageRows.length >= 20}
                    onClick={() => {
                      const id = nextPackageRowId.current++;
                      setPackageRows((current) => [...current, emptyPackageRow(id)]);
                    }}
                  >
                    <Plus size={16} />
                    إضافة باكيدج أخرى
                  </Button>
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
                    variant="outline"
                    disabled={pulseRows.length >= devices.length}
                    onClick={() => {
                      const device = devices.find((item) => !selectedDeviceKeys.includes(item.device_key));
                      if (!device) return;
                      const id = nextPulseRowId.current++;
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
