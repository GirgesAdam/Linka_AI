"use client";

import Link from "next/link";
import { useActionState, useEffect, useState } from "react";
import { Plus, Trash2, UserRoundPlus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice, Service, ServicePackageOffer } from "@/lib/types";
import { getActiveOffersForService, parseEgpAmountToMinor } from "./registration-utils";
import { registerPatientAction } from "./actions";
import { PatientOpeningDataEditor } from "./patient-opening-data-editor";

type RegistrationState = {
  error: string | null;
  existingPatient?: { id: string; name: string } | null;
};
type PulseRow = { id: number; deviceKey: string; pulsesRemaining: string };

const copy = {
  title: "\u062a\u0633\u062c\u064a\u0644 \u0639\u0645\u064a\u0644 \u062c\u062f\u064a\u062f",
  description: "\u0623\u062f\u062e\u0644 \u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0639\u0645\u064a\u0644\u060c \u0648\u0623\u0636\u0641 \u0623\u0631\u0635\u062f\u0629 \u0627\u0641\u062a\u062a\u0627\u062d\u064a\u0629 \u0625\u0630\u0627 \u0644\u0632\u0645.",
  open: "\u062a\u0633\u062c\u064a\u0644 \u0639\u0645\u064a\u0644 \u062c\u062f\u064a\u062f",
  close: "\u0625\u063a\u0644\u0627\u0642",
  firstName: "\u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u0623\u0648\u0644 *",
  lastName: "\u0627\u0633\u0645 \u0627\u0644\u0639\u0627\u0626\u0644\u0629",
  phone: "\u0631\u0642\u0645 \u0627\u0644\u0645\u0648\u0628\u0627\u064a\u0644 *",
  phoneHint: "\u0623\u062f\u062e\u0644 11 \u0631\u0642\u0645\u064b\u0627 \u064a\u0628\u062f\u0623 \u0628\u0640 010 \u0623\u0648 011 \u0623\u0648 012 \u0623\u0648 015",
  customerId: "Customer ID (\u0627\u062e\u062a\u064a\u0627\u0631\u064a)",
  packageSection: "\u0631\u0635\u064a\u062f \u0627\u0644\u0628\u0627\u0643\u062f\u062c (\u0627\u062e\u062a\u064a\u0627\u0631\u064a)",
  packageHelp: "\u0627\u062e\u062a\u0631 \u0627\u0644\u062e\u062f\u0645\u0629 \u0623\u0648\u0644\u064b\u0627\u060c \u062b\u0645 \u0627\u0644\u0628\u0627\u0643\u062f\u062c \u0627\u0644\u0645\u0641\u0639\u0651\u0644. \u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0648\u0633\u0639\u0631 \u0627\u0644\u0628\u0627\u0643\u062f\u062c \u0645\u0623\u062e\u0648\u0630\u0627\u0646 \u0645\u0646 \u0627\u0644\u0639\u0631\u0636 \u0627\u0644\u0645\u062e\u062a\u0627\u0631.",
  service: "\u0627\u0644\u062e\u062f\u0645\u0629",
  chooseService: "\u0627\u062e\u062a\u0631 \u0627\u0644\u062e\u062f\u0645\u0629 \u0623\u0648\u0644\u064b\u0627",
  packageOffer: "\u0639\u0631\u0636 \u0627\u0644\u0628\u0627\u0643\u062f\u062c",
  noPackage: "\u0628\u062f\u0648\u0646 \u0631\u0635\u064a\u062f \u0628\u0627\u0643\u062f\u062c",
  noOffers: "\u0644\u0627 \u062a\u0648\u062c\u062f \u0639\u0631\u0648\u0636 \u0628\u0627\u0643\u062f\u062c \u0645\u0641\u0639\u0651\u0644\u0629 \u0644\u0647\u0630\u0647 \u0627\u0644\u062e\u062f\u0645\u0629.",
  sessionsRemaining: "\u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629",
  amountPaid: "\u0627\u0644\u0645\u0628\u0644\u063a \u0627\u0644\u0645\u062f\u0641\u0648\u0639 (\u062c\u0646\u064a\u0647)",
  packagePrice: "\u0633\u0639\u0631 \u0627\u0644\u0628\u0627\u0643\u062f\u062c",
  outstanding: "\u0627\u0644\u0645\u062a\u0628\u0642\u064a",
  paymentMethod: "\u0637\u0631\u064a\u0642\u0629 \u0627\u0644\u062f\u0641\u0639",
  pulseSection: "\u0631\u0635\u064a\u062f Pulse (\u0627\u062e\u062a\u064a\u0627\u0631\u064a)",
  pulseHelp: "\u0623\u0636\u0641 \u0631\u0635\u064a\u062f \u0627\u0644\u0646\u0628\u0636\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629 \u0644\u0643\u0644 \u062c\u0647\u0627\u0632 \u0628\u0634\u0643\u0644 \u0645\u0633\u062a\u0642\u0644. \u0644\u0627 \u064a\u062a\u0645 \u062a\u0633\u062c\u064a\u0644 \u0623\u0633\u0639\u0627\u0631 \u0623\u0648 \u0645\u062f\u0641\u0648\u0639\u0627\u062a \u0623\u0648 \u0627\u0633\u062a\u062e\u062f\u0627\u0645\u0627\u062a \u062a\u0627\u0631\u064a\u062e\u064a\u0629.",
  device: "\u062c\u0647\u0627\u0632 \u0627\u0644\u0644\u064a\u0632\u0631",
  chooseDevice: "\u0627\u062e\u062a\u0631 \u0627\u0644\u062c\u0647\u0627\u0632",
  noPulse: "\u0628\u062f\u0648\u0646 \u0631\u0635\u064a\u062f Pulse",
  pulsesRemaining: "\u0627\u0644\u0646\u0628\u0636\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629",
  addDevice: "\u0625\u0636\u0627\u0641\u0629 \u062c\u0647\u0627\u0632",
  removeDevice: "\u062d\u0630\u0641 \u0627\u0644\u062c\u0647\u0627\u0632",
  noDevices: "\u0644\u0627 \u062a\u0648\u062c\u062f \u0623\u062c\u0647\u0632\u0629 \u0644\u064a\u0632\u0631 \u0645\u0641\u0639\u0651\u0644\u0629.",
  pending: "\u062c\u0627\u0631\u064d \u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644...",
  submit: "\u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644",
};

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
  const [selectedServiceId, setSelectedServiceId] = useState("");
  const [selectedOfferId, setSelectedOfferId] = useState("");
  const [amountPaidEgp, setAmountPaidEgp] = useState("");
  const [pulseRows, setPulseRows] = useState<PulseRow[]>([
    { id: 0, deviceKey: "", pulsesRemaining: "" },
  ]);
  const [nextPulseRowId, setNextPulseRowId] = useState(1);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsOpen(false);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  const activeServices = services.filter((service) => service.is_active);
  const serviceOffers = getActiveOffersForService(offers, selectedServiceId);
  const selectedOffer = serviceOffers.find((offer) => offer.id === selectedOfferId);
  const selectedDeviceKeys = pulseRows.map((row) => row.deviceKey).filter(Boolean);
  let paidMinorPreview = 0;
  try {
    paidMinorPreview = parseEgpAmountToMinor(amountPaidEgp);
  } catch {
    paidMinorPreview = 0;
  }
  const outstandingPreview = selectedOffer
    ? Math.max(0, selectedOffer.price_minor - paidMinorPreview)
    : 0;

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
            onClick={() => setIsOpen(false)}
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
                onClick={() => setIsOpen(false)}
                className="grid size-9 shrink-0 place-items-center rounded-lg border border-[var(--border)] text-slate-600 hover:bg-slate-50"
              >
                <X size={17} />
              </button>
            </header>

            <form action={formAction} className="space-y-4 p-5">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                  {copy.firstName}
                  <Input name="first_name" autoComplete="given-name" maxLength={120} required />
                </label>
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                  {copy.lastName}
                  <Input name="last_name" autoComplete="family-name" maxLength={120} />
                </label>
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                  {copy.phone}
                  <Input
                    name="phone"
                    type="tel"
                    inputMode="numeric"
                    autoComplete="tel-national"
                    dir="ltr"
                    placeholder="01012345678"
                    pattern="01[0125][0-9]{8}"
                    title={copy.phoneHint}
                    maxLength={11}
                    required
                  />
                </label>
                <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                  {copy.customerId}
                  <Input name="customer_id" maxLength={200} dir="ltr" />
                </label>
              </div>

              <details className="rounded-xl border border-[var(--border)] p-3">
                <summary className="min-h-10 cursor-pointer content-center text-sm font-bold text-[var(--interactive)]">
                  {copy.packageSection}
                </summary>
                <div className="mt-3 space-y-3">
                  <p className="text-xs leading-5 text-[var(--muted)]">{copy.packageHelp}</p>
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    {copy.service}
                    <select
                      value={selectedServiceId}
                      onChange={(event) => {
                        setSelectedServiceId(event.target.value);
                        setSelectedOfferId("");
                        setAmountPaidEgp("");
                      }}
                      className="form-control h-10 min-h-10"
                    >
                      <option value="">{copy.chooseService}</option>
                      {activeServices.map((service) => (
                        <option key={service.id} value={service.id}>{service.name}</option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    {copy.packageOffer}
                    <select
                      name="initial_package_offer_id"
                      value={selectedOfferId}
                      disabled={!selectedServiceId}
                      onChange={(event) => {
                        const offerId = event.target.value;
                        const offer = serviceOffers.find((item) => item.id === offerId);
                        setSelectedOfferId(offerId);
                        setAmountPaidEgp(offer ? (offer.price_minor / 100).toFixed(2) : "");
                      }}
                      className="form-control h-10 min-h-10"
                    >
                      <option value="">{copy.noPackage}</option>
                      {serviceOffers.map((offer) => (
                        <option key={offer.id} value={offer.id}>
                          {offer.device_name ? offer.device_name + " - " : ""}
                          {offer.sessions_count} {"\u062c\u0644\u0633\u0629"} - {(offer.price_minor / 100).toFixed(2)} EGP
                        </option>
                      ))}
                    </select>
                  </label>
                  {selectedServiceId && serviceOffers.length === 0 && (
                    <p className="rounded-lg bg-[var(--surface-2)] p-3 text-xs text-[var(--muted)]">{copy.noOffers}</p>
                  )}
                  {selectedOffer && (
                    <>
                      <div className="grid gap-2 rounded-lg bg-[var(--surface-2)] p-3 text-sm sm:grid-cols-2">
                        <div>
                          <div className="text-xs text-[var(--muted)]">{copy.packagePrice}</div>
                          <div className="mt-1 font-bold">{(selectedOffer.price_minor / 100).toFixed(2)} EGP</div>
                        </div>
                        <div>
                          <div className="text-xs text-[var(--muted)]">{copy.outstanding}</div>
                          <div className="mt-1 font-bold">{(outstandingPreview / 100).toFixed(2)} EGP</div>
                        </div>
                      </div>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                          {copy.sessionsRemaining}
                          <Input
                            name="initial_package_sessions_remaining"
                            type="number"
                            min={1}
                            max={selectedOffer.sessions_count}
                            step={1}
                            required
                            placeholder={"1 - " + selectedOffer.sessions_count}
                          />
                        </label>
                        <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                          {copy.amountPaid}
                          <Input
                            name="initial_package_amount_paid_egp"
                            type="number"
                            min={0}
                            max={(selectedOffer.price_minor / 100).toFixed(2)}
                            step="0.01"
                            value={amountPaidEgp}
                            onChange={(event) => setAmountPaidEgp(event.target.value)}
                            required
                          />
                        </label>
                      </div>
                      <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                        {copy.paymentMethod}
                        <select name="initial_package_payment_method" defaultValue="cash" className="form-control h-10 min-h-10">
                          <option value="cash">Cash</option>
                          <option value="visa">Visa</option>
                          <option value="instapay">InstaPay</option>
                        </select>
                      </label>
                    </>
                  )}
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
                                  item.id === row.id
                                    ? { ...item, deviceKey: event.target.value, pulsesRemaining: "" }
                                    : item,
                                ))}
                                className="form-control h-10 min-h-10"
                              >
                                <option value="">{copy.noPulse}</option>
                                {availableDevices.map((device) => (
                                  <option key={device.id} value={device.device_key}>{device.name}</option>
                                ))}
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
                                aria-label={copy.removeDevice + " " + (index + 1)}
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

              {state.error && (
                <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm font-medium text-red-800">
                  <p>{state.error}</p>
                  {state.existingPatient && (
                    <Link
                      href={`/patients/${state.existingPatient.id}`}
                      className="mt-2 inline-block font-bold text-[var(--interactive)] underline underline-offset-2"
                    >
                      {state.existingPatient.name}
                    </Link>
                  )}
                </div>
              )}

              {state.existingPatient && (
                <PatientOpeningDataEditor
                  patientId={state.existingPatient.id}
                  offers={offers}
                  devices={devices}
                  services={services}
                />
              )}

              <div className="flex justify-end gap-2">
                <Button type="button" onClick={() => setIsOpen(false)}>{copy.close}</Button>
                <Button type="submit" disabled={pending}>
                  {pending ? copy.pending : copy.submit}
                </Button>
              </div>
            </form>
          </section>
        </div>
      )}
    </>
  );
}
