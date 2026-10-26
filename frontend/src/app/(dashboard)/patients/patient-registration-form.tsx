"use client";

import Link from "next/link";
import { useActionState, useCallback, useEffect, useState } from "react";
import { Plus, Trash2, UserRoundPlus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice, Service, ServicePackageOffer } from "@/lib/types";
import {
  getActiveOffersForService,
  isCurrentDuplicateRegistration,
  parseEgpAmountToMinor,
} from "./registration-utils";
import { registerPatientAction } from "./actions";
import { PatientOpeningDataEditor } from "./patient-opening-data-editor";

type RegistrationState = {
  error: string | null;
  existingPatient?: { id: string; name: string } | null;
  duplicatePhone?: string | null;
  duplicateAttempt?: string | null;
};
type PackageRow = {
  id: number;
  serviceId: string;
  offerId: string;
  sessionsRemaining: string;
  amountPaidEgp: string;
  paymentMethod: "cash" | "visa" | "instapay";
};
type PulseRow = { id: number; deviceKey: string; pulsesRemaining: string };

const copy = {
  title: "تسجيل عميل",
  description: "أدخل بيانات العميل، وأضف أرصدة افتتاحية إذا لزم.",
  open: "تسجيل عميل",
  close: "إغلاق",
  name: "الاسم *",
  phone: "رقم الموبايل *",
  phoneHint: "أدخل 11 رقمًا يبدأ بـ 010 أو 011 أو 012 أو 015",
  id: "ID (اختياري)",
  packageSection: "أرصدة الباكيدجات (اختياري)",
  packageHelp: "يمكن إضافة أكثر من باكيدج، ولكل باكيدج رصيد ومدفوعات مستقلة.",
  service: "الخدمة",
  chooseService: "اختر الخدمة",
  packageOffer: "الباكيدج",
  choosePackage: "اختر الباكيدج",
  noOffers: "لا توجد عروض باكيدج مفعّلة لهذه الخدمة.",
  sessionsRemaining: "الجلسات المتبقية",
  amountPaid: "المبلغ المدفوع (جنيه)",
  packagePrice: "سعر الباكيدج",
  outstanding: "المتبقي",
  paymentMethod: "طريقة الدفع",
  addPackage: "إضافة باكيدج",
  removePackage: "حذف الباكيدج",
  pulseSection: "رصيد Pulse (اختياري)",
  pulseHelp: "أضف رصيد النبضات المتبقية لكل جهاز بشكل مستقل. لا يتم تسجيل أسعار أو مدفوعات أو استخدامات تاريخية.",
  device: "جهاز الليزر",
  noPulse: "بدون رصيد Pulse",
  pulsesRemaining: "النبضات المتبقية",
  addDevice: "إضافة جهاز",
  removeDevice: "حذف الجهاز",
  noDevices: "لا توجد أجهزة ليزر مفعّلة.",
  pending: "جارٍ تسجيل العميل...",
  submit: "تسجيل العميل",
};

function emptyPackageRow(id: number): PackageRow {
  return { id, serviceId: "", offerId: "", sessionsRemaining: "", amountPaidEgp: "", paymentMethod: "cash" };
}

export function PatientRegistrationForm({
  offers,
  devices,
  services,
}: {
  offers: ServicePackageOffer[];
  devices: ClinicLaserDevice[];
  services: Service[];
}) {
  const [state, formAction, pending] = useActionState<RegistrationState, FormData>(
    registerPatientAction,
    { error: null, existingPatient: null },
  );
  const [isOpen, setIsOpen] = useState(false);
  const [phone, setPhone] = useState("");
  const [duplicateAttempt, setDuplicateAttempt] = useState(0);
  const [packageRows, setPackageRows] = useState<PackageRow[]>([emptyPackageRow(0)]);
  const [nextPackageRowId, setNextPackageRowId] = useState(1);
  const [pulseRows, setPulseRows] = useState<PulseRow[]>([
    { id: 0, deviceKey: "", pulsesRemaining: "" },
  ]);
  const [nextPulseRowId, setNextPulseRowId] = useState(1);

  const closeRegistrationModal = useCallback(() => {
    setIsOpen(false);
    setPhone("");
    setDuplicateAttempt((current) => current + 1);
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRegistrationModal();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, closeRegistrationModal]);

  const activeServices = services.filter((service) => service.is_active);
  const selectedOfferIds = packageRows.map((row) => row.offerId).filter(Boolean);
  const selectedDeviceKeys = pulseRows.map((row) => row.deviceKey).filter(Boolean);
  const activeDuplicatePatient = state.existingPatient && isCurrentDuplicateRegistration(
    state.duplicatePhone,
    state.duplicateAttempt,
    phone,
    String(duplicateAttempt),
  )
    ? state.existingPatient
    : null;
  const visibleError = state.existingPatient ? (activeDuplicatePatient ? state.error : null) : state.error;
  const packagePayload = packageRows.map((row) => ({
    offer_id: row.offerId,
    sessions_remaining: row.sessionsRemaining,
    amount_paid_egp: row.amountPaidEgp,
    payment_method: row.paymentMethod,
  }));

  return (
    <>
      <Button type="button" onClick={() => setIsOpen(true)} aria-haspopup="dialog">
        <UserRoundPlus size={17} />
        {copy.open}
      </Button>

      {isOpen && (
        <div className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4">
          <button
            type="button"
            aria-label={copy.close}
            className="absolute inset-0 bg-slate-950/35 backdrop-blur-[1px]"
            onClick={closeRegistrationModal}
          />
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="patient-registration-title"
            className="relative z-10 max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-t-3xl bg-white shadow-xl sm:rounded-3xl"
          >
            <header className="sticky top-0 z-20 flex items-start justify-between gap-4 border-b border-[var(--border)] bg-white px-5 py-4">
              <div>
                <h2 id="patient-registration-title" className="flex items-center gap-2 text-lg font-black">
                  <UserRoundPlus size={19} />
                  {copy.title}
                </h2>
                <p className="mt-1 text-sm leading-6 text-[var(--muted)]">{copy.description}</p>
              </div>
              <button
                type="button"
                aria-label={copy.close}
                onClick={closeRegistrationModal}
                className="grid size-9 shrink-0 place-items-center rounded-lg border border-[var(--border)] text-slate-600 hover:bg-slate-50"
              >
                <X size={17} />
              </button>
            </header>

            <form action={formAction} className="space-y-4 p-5">
              <input type="hidden" name="duplicate_attempt" value={String(duplicateAttempt)} />
              <input type="hidden" name="initial_packages_json" value={JSON.stringify(packagePayload)} />
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                  {copy.name}
                  <Input name="name" autoComplete="name" maxLength={120} required />
                </label>
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                  {copy.phone}
                  <Input
                    name="phone"
                    type="tel"
                    inputMode="numeric"
                    autoComplete="tel-national"
                    dir="ltr"
                    value={phone}
                    onChange={(event) => setPhone(event.target.value)}
                    placeholder="01012345678"
                    pattern="01[0125][0-9]{8}"
                    title={copy.phoneHint}
                    maxLength={11}
                    required
                  />
                </label>
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700 sm:col-span-2">
                  {copy.id}
                  <Input name="customer_id" maxLength={200} dir="ltr" />
                </label>
              </div>

              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="min-h-10 cursor-pointer content-center text-sm font-bold text-[var(--interactive)]">
                  {copy.packageSection}
                </summary>
                <div className="mt-3 space-y-3">
                  <p className="text-xs leading-5 text-[var(--muted)]">{copy.packageHelp}</p>
                  {packageRows.map((row, index) => {
                    const serviceOffers = getActiveOffersForService(offers, row.serviceId);
                    const availableOffers = serviceOffers.filter(
                      (offer) => offer.id === row.offerId || !selectedOfferIds.includes(offer.id),
                    );
                    const selectedOffer = serviceOffers.find((offer) => offer.id === row.offerId);
                    let paidMinorPreview = 0;
                    try {
                      paidMinorPreview = parseEgpAmountToMinor(row.amountPaidEgp || "0");
                    } catch {
                      paidMinorPreview = 0;
                    }
                    const outstandingPreview = selectedOffer
                      ? Math.max(0, selectedOffer.price_minor - paidMinorPreview)
                      : 0;
                    return (
                      <div key={row.id} className="space-y-3 rounded-xl bg-[var(--surface-2)] p-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="text-sm font-black">باكيدج {index + 1}</div>
                          {packageRows.length > 1 && (
                            <button
                              type="button"
                              aria-label={`${copy.removePackage} ${index + 1}`}
                              onClick={() => setPackageRows((current) => current.filter((item) => item.id !== row.id))}
                              className="rounded-lg p-2 text-slate-500 hover:bg-white hover:text-red-700"
                            >
                              <Trash2 size={17} />
                            </button>
                          )}
                        </div>
                        <div className="grid gap-3 sm:grid-cols-2">
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            {copy.service}
                            <select
                              value={row.serviceId}
                              onChange={(event) => setPackageRows((current) => current.map((item) =>
                                item.id === row.id
                                  ? { ...item, serviceId: event.target.value, offerId: "", sessionsRemaining: "", amountPaidEgp: "" }
                                  : item,
                              ))}
                              className="form-control h-10 min-h-10"
                            >
                              <option value="">{copy.chooseService}</option>
                              {activeServices.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}
                            </select>
                          </label>
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            {copy.packageOffer}
                            <select
                              value={row.offerId}
                              disabled={!row.serviceId}
                              onChange={(event) => {
                                const offerId = event.target.value;
                                const offer = serviceOffers.find((item) => item.id === offerId);
                                setPackageRows((current) => current.map((item) =>
                                  item.id === row.id
                                    ? {
                                        ...item,
                                        offerId,
                                        sessionsRemaining: "",
                                        amountPaidEgp: offer ? (offer.price_minor / 100).toFixed(2) : "",
                                      }
                                    : item,
                                ));
                              }}
                              className="form-control h-10 min-h-10"
                            >
                              <option value="">{copy.choosePackage}</option>
                              {availableOffers.map((offer) => (
                                <option key={offer.id} value={offer.id}>
                                  {offer.device_name ? `${offer.device_name} - ` : ""}{offer.sessions_count} جلسة - {(offer.price_minor / 100).toFixed(2)} EGP
                                </option>
                              ))}
                            </select>
                          </label>
                        </div>
                        {row.serviceId && serviceOffers.length === 0 && (
                          <p className="rounded-lg bg-white p-3 text-xs text-[var(--muted)]">{copy.noOffers}</p>
                        )}
                        <div className="grid gap-3 sm:grid-cols-3">
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            {copy.sessionsRemaining}
                            <Input
                              type="number"
                              min={1}
                              max={selectedOffer?.sessions_count}
                              step={1}
                              value={row.sessionsRemaining}
                              disabled={!selectedOffer}
                              onChange={(event) => setPackageRows((current) => current.map((item) =>
                                item.id === row.id ? { ...item, sessionsRemaining: event.target.value } : item,
                              ))}
                            />
                          </label>
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            {copy.amountPaid}
                            <Input
                              type="number"
                              min={0}
                              max={selectedOffer ? (selectedOffer.price_minor / 100).toFixed(2) : undefined}
                              step="0.01"
                              value={row.amountPaidEgp}
                              disabled={!selectedOffer}
                              onChange={(event) => setPackageRows((current) => current.map((item) =>
                                item.id === row.id ? { ...item, amountPaidEgp: event.target.value } : item,
                              ))}
                            />
                          </label>
                          <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                            {copy.paymentMethod}
                            <select
                              value={row.paymentMethod}
                              disabled={!selectedOffer}
                              onChange={(event) => setPackageRows((current) => current.map((item) =>
                                item.id === row.id
                                  ? { ...item, paymentMethod: event.target.value as PackageRow["paymentMethod"] }
                                  : item,
                              ))}
                              className="form-control h-10 min-h-10"
                            >
                              <option value="cash">Cash</option>
                              <option value="visa">Visa</option>
                              <option value="instapay">InstaPay</option>
                            </select>
                          </label>
                        </div>
                        {selectedOffer && (
                          <div className="grid gap-2 rounded-lg bg-white p-3 text-sm sm:grid-cols-2">
                            <div><div className="text-xs text-[var(--muted)]">{copy.packagePrice}</div><div className="mt-1 font-bold">{(selectedOffer.price_minor / 100).toFixed(2)} EGP</div></div>
                            <div><div className="text-xs text-[var(--muted)]">{copy.outstanding}</div><div className="mt-1 font-bold">{(outstandingPreview / 100).toFixed(2)} EGP</div></div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                  <Button
                    type="button"
                    variant="outline"
                    disabled={packageRows.length >= 20}
                    onClick={() => {
                      setPackageRows((current) => [...current, emptyPackageRow(nextPackageRowId)]);
                      setNextPackageRowId((current) => current + 1);
                    }}
                  >
                    <Plus size={16} />
                    {copy.addPackage}
                  </Button>
                </div>
              </details>

              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="min-h-10 cursor-pointer content-center text-sm font-bold text-[var(--interactive)]">
                  {copy.pulseSection}
                </summary>
                <div className="mt-3 space-y-3">
                  <p className="text-xs leading-5 text-[var(--muted)]">{copy.pulseHelp}</p>
                  {devices.length === 0 ? (
                    <p className="rounded-lg bg-[var(--surface-2)] p-3 text-xs text-[var(--muted)]">{copy.noDevices}</p>
                  ) : (
                    <>
                      {pulseRows.map((row, index) => {
                        const availableDevices = devices.filter(
                          (device) => device.device_key === row.deviceKey || !selectedDeviceKeys.includes(device.device_key),
                        );
                        return (
                          <div key={row.id} className="grid gap-3 rounded-lg bg-[var(--surface-2)] p-3 sm:grid-cols-[1fr_1fr_auto]">
                            <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                              {copy.device} {index + 1}
                              <select
                                name="initial_pulse_device_key"
                                value={row.deviceKey}
                                onChange={(event) => setPulseRows((current) => current.map((item) =>
                                  item.id === row.id ? { ...item, deviceKey: event.target.value, pulsesRemaining: "" } : item,
                                ))}
                                className="form-control h-10 min-h-10"
                              >
                                <option value="">{copy.noPulse}</option>
                                {availableDevices.map((device) => <option key={device.id} value={device.device_key}>{device.name}</option>)}
                              </select>
                            </label>
                            <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                              {copy.pulsesRemaining}
                              <Input
                                name="initial_pulses_remaining"
                                type="number"
                                min={1}
                                max={1_000_000_000}
                                step={1}
                                value={row.pulsesRemaining}
                                onChange={(event) => setPulseRows((current) => current.map((item) =>
                                  item.id === row.id ? { ...item, pulsesRemaining: event.target.value } : item,
                                ))}
                                required={Boolean(row.deviceKey)}
                              />
                            </label>
                            {pulseRows.length > 1 && (
                              <button
                                type="button"
                                aria-label={`${copy.removeDevice} ${index + 1}`}
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
                          setPulseRows((current) => [...current, { id: nextPulseRowId, deviceKey: "", pulsesRemaining: "" }]);
                          setNextPulseRowId((current) => current + 1);
                        }}
                      >
                        <Plus size={16} />
                        {copy.addDevice}
                      </Button>
                    </>
                  )}
                </div>
              </details>

              {visibleError && (
                <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm font-medium text-red-800">
                  <p>{visibleError}</p>
                  {activeDuplicatePatient && (
                    <Link
                      href={`/patients/${activeDuplicatePatient.id}`}
                      className="mt-2 inline-block font-bold text-[var(--interactive)] underline underline-offset-2"
                    >
                      {activeDuplicatePatient.name}
                    </Link>
                  )}
                </div>
              )}

              {activeDuplicatePatient && (
                <PatientOpeningDataEditor
                  key={activeDuplicatePatient.id}
                  patientId={activeDuplicatePatient.id}
                  offers={offers}
                  devices={devices}
                  services={services}
                />
              )}

              <div className="flex justify-end gap-2">
                <Button type="button" onClick={closeRegistrationModal}>{copy.close}</Button>
                <Button type="submit" disabled={pending}>{pending ? copy.pending : copy.submit}</Button>
              </div>
            </form>
          </section>
        </div>
      )}
    </>
  );
}
