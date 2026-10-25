"use client";

import { useActionState, useState } from "react";
import { UserRoundPlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { ClinicLaserDevice, ServicePackageOffer } from "@/lib/types";
import { registerPatientAction } from "./actions";

type RegistrationState = { error: string | null };

const copy = {
  title: "\u062a\u0633\u062c\u064a\u0644 \u0639\u0645\u064a\u0644 \u062c\u062f\u064a\u062f",
  description: "\u0623\u062f\u062e\u0644 \u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0639\u0645\u064a\u0644. \u064a\u0645\u0643\u0646 \u062a\u0633\u062c\u064a\u0644 \u0631\u0635\u064a\u062f \u0628\u0627\u0643\u062f\u062c \u0623\u0648 Pulse \u062d\u0627\u0644\u064a \u0628\u062f\u0648\u0646 \u0625\u0646\u0634\u0627\u0621 \u0639\u0645\u0644\u064a\u0629 \u0628\u064a\u0639 \u0623\u0648 \u062f\u0641\u0639\u0629 \u0645\u0627\u0644\u064a\u0629.",
  firstName: "\u0627\u0644\u0627\u0633\u0645 \u0627\u0644\u0623\u0648\u0644 *",
  lastName: "\u0627\u0633\u0645 \u0627\u0644\u0639\u0627\u0626\u0644\u0629",
  phone: "\u0631\u0642\u0645 \u0627\u0644\u0645\u0648\u0628\u0627\u064a\u0644 *",
  phoneHint: "\u0623\u062f\u062e\u0644 11 \u0631\u0642\u0645\u064b\u0627 \u064a\u0628\u062f\u0623 \u0628\u0640 010 \u0623\u0648 011 \u0623\u0648 012 \u0623\u0648 015",
  customerId: "Customer ID (\u0627\u062e\u062a\u064a\u0627\u0631\u064a)",
  openingBalance: "\u0625\u0636\u0627\u0641\u0629 \u0631\u0635\u064a\u062f \u0627\u0641\u062a\u062a\u0627\u062d\u064a (\u0627\u062e\u062a\u064a\u0627\u0631\u064a)",
  packageBalance: "\u0631\u0635\u064a\u062f \u0627\u0644\u0628\u0627\u0643\u062f\u062c",
  packageHelp: "\u0627\u062e\u062a\u0631 \u062a\u0639\u0631\u064a\u0641 \u0627\u0644\u0628\u0627\u0643\u062f\u062c \u0627\u0644\u0645\u0633\u062c\u0644 \u0641\u064a \u0627\u0644\u0639\u064a\u0627\u062f\u0629\u060c \u062b\u0645 \u0623\u062f\u062e\u0644 \u0639\u062f\u062f \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629. \u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u062c\u0644\u0633\u0627\u062a \u064a\u0624\u062e\u0630 \u0645\u0646 \u062a\u0639\u0631\u064a\u0641 \u0627\u0644\u0628\u0627\u0643\u062f\u062c.",
  packageOffer: "\u062a\u0639\u0631\u064a\u0641 \u0627\u0644\u0628\u0627\u0643\u062f\u062c",
  noPackage: "\u0628\u062f\u0648\u0646 \u0631\u0635\u064a\u062f \u0628\u0627\u0643\u062f\u062c",
  sessionUnit: "\u062c\u0644\u0633\u0629",
  sessionsRemaining: "\u0627\u0644\u062c\u0644\u0633\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629",
  from: "\u0645\u0646",
  to: "\u0625\u0644\u0649",
  choosePackage: "\u0627\u062e\u062a\u0631 \u0627\u0644\u0628\u0627\u0643\u062f\u062c \u0623\u0648\u0644\u064b\u0627",
  noOffers: "\u0644\u0627 \u062a\u0648\u062c\u062f \u0628\u0627\u0643\u062f\u062c\u0627\u062a \u0645\u0641\u0639\u0651\u0644\u0629. \u064a\u0645\u0643\u0646\u0643 \u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644 \u0628\u062f\u0648\u0646 \u0631\u0635\u064a\u062f \u0628\u0627\u0643\u062f\u062c.",
  pulseBalance: "\u0631\u0635\u064a\u062f Pulse",
  pulseHelp: "\u0633\u062c\u0651\u0644 \u0639\u062f\u062f \u0627\u0644\u0646\u0628\u0636\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629 \u0639\u0644\u0649 \u0627\u0644\u062c\u0647\u0627\u0632. \u0644\u0646 \u064a\u062a\u0645 \u0625\u0646\u0634\u0627\u0621 \u0645\u062f\u0641\u0648\u0639\u0627\u062a \u0623\u0648 \u0627\u0633\u062a\u062e\u062f\u0627\u0645\u0627\u062a \u062a\u0627\u0631\u064a\u062e\u064a\u0629 \u0648\u0647\u0645\u064a\u0629.",
  device: "\u062c\u0647\u0627\u0632 \u0627\u0644\u0644\u064a\u0632\u0631",
  noPulse: "\u0628\u062f\u0648\u0646 \u0631\u0635\u064a\u062f Pulse",
  pulsesRemaining: "\u0627\u0644\u0646\u0628\u0636\u0627\u062a \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629",
  pulseCount: "\u0639\u062f\u062f \u0627\u0644\u0646\u0628\u0636\u0627\u062a",
  chooseDevice: "\u0627\u062e\u062a\u0631 \u0627\u0644\u062c\u0647\u0627\u0632 \u0623\u0648\u0644\u064b\u0627",
  noDevices: "\u0644\u0627 \u062a\u0648\u062c\u062f \u0623\u062c\u0647\u0632\u0629 \u0644\u064a\u0632\u0631 \u0645\u0641\u0639\u0651\u0644\u0629. \u064a\u0645\u0643\u0646\u0643 \u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644 \u0628\u062f\u0648\u0646 \u0631\u0635\u064a\u062f Pulse.",
  pending: "\u062c\u0627\u0631\u064d \u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644...",
  submit: "\u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u0639\u0645\u064a\u0644",
};

export function PatientRegistrationForm({
  offers,
  devices,
}: {
  offers: ServicePackageOffer[];
  devices: ClinicLaserDevice[];
}) {
  const [state, formAction, pending] = useActionState<RegistrationState, FormData>(
    registerPatientAction,
    { error: null },
  );
  const [selectedOfferId, setSelectedOfferId] = useState("");
  const [selectedPulseDeviceKey, setSelectedPulseDeviceKey] = useState("");
  const selectedOffer = offers.find((offer) => offer.id === selectedOfferId);

  return (
    <Card className="mb-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UserRoundPlus size={18} />
          {copy.title}
        </CardTitle>
        <p className="text-sm leading-6 text-[var(--muted)]">{copy.description}</p>
      </CardHeader>
      <CardContent>
        <form action={formAction} className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
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
              {copy.openingBalance}
            </summary>
            <div className="mt-3 space-y-4">
              <section className="rounded-lg bg-[var(--surface-2)] p-3">
                <h3 className="text-sm font-bold">{copy.packageBalance}</h3>
                <p className="mt-1 text-xs leading-5 text-[var(--muted)]">{copy.packageHelp}</p>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    {copy.packageOffer}
                    <select
                      name="initial_package_offer_id"
                      value={selectedOfferId}
                      onChange={(event) => setSelectedOfferId(event.target.value)}
                      className="form-control h-10 min-h-10"
                    >
                      <option value="">{copy.noPackage}</option>
                      {offers.map((offer) => (
                        <option key={offer.id} value={offer.id}>
                          {offer.service_name}
                          {offer.device_name ? ` - ${offer.device_name}` : ""}
                          {" - "}
                          {offer.sessions_count} {copy.sessionUnit}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    {copy.sessionsRemaining}
                    <Input
                      name="initial_package_sessions_remaining"
                      type="number"
                      min={1}
                      max={selectedOffer?.sessions_count}
                      step={1}
                      required={Boolean(selectedOfferId)}
                      disabled={!selectedOfferId}
                      placeholder={selectedOffer ? `${copy.from} 1 ${copy.to} ${selectedOffer.sessions_count}` : copy.choosePackage}
                    />
                  </label>
                </div>
                {offers.length === 0 && (
                  <p className="mt-2 text-xs text-[var(--muted)]">{copy.noOffers}</p>
                )}
              </section>

              <section className="rounded-lg bg-[var(--surface-2)] p-3">
                <h3 className="text-sm font-bold">{copy.pulseBalance}</h3>
                <p className="mt-1 text-xs leading-5 text-[var(--muted)]">{copy.pulseHelp}</p>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    {copy.device}
                    <select
                      name="initial_pulse_device_key"
                      value={selectedPulseDeviceKey}
                      onChange={(event) => setSelectedPulseDeviceKey(event.target.value)}
                      className="form-control h-10 min-h-10"
                    >
                      <option value="">{copy.noPulse}</option>
                      {devices.map((device) => (
                        <option key={device.id} value={device.device_key}>
                          {device.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-sm font-semibold text-slate-700">
                    {copy.pulsesRemaining}
                    <Input
                      name="initial_pulses_remaining"
                      type="number"
                      min={1}
                      step={1}
                      required={Boolean(selectedPulseDeviceKey)}
                      disabled={!selectedPulseDeviceKey}
                      placeholder={selectedPulseDeviceKey ? copy.pulseCount : copy.chooseDevice}
                    />
                  </label>
                </div>
                {devices.length === 0 && (
                  <p className="mt-2 text-xs text-[var(--muted)]">{copy.noDevices}</p>
                )}
              </section>
            </div>
          </details>

          {state.error && (
            <p role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm font-medium text-red-800">
              {state.error}
            </p>
          )}

          <div className="flex justify-end">
            <Button type="submit" disabled={pending}>
              {pending ? copy.pending : copy.submit}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
