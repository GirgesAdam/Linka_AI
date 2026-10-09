import Link from "next/link";
import { Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PATIENT_PHONE_ERROR } from "@/lib/patient-phone";
import { formatDateTime } from "@/lib/format";
import { appointmentLabels } from "@/lib/status";
import type { Appointment, Doctor, Patient, PatientPackage, PulseBalance, Service, Staff } from "@/lib/types";
import { ManualAppointmentForm } from "./manual-appointment-form";

type ScheduleColumnId = string;

type DevicePrice = {
  service_id: string;
  device_key: string;
  device_name: string;
  price_minor: number | null;
  duration_minutes: number | null;
  currency: string;
  configured: boolean;
};

function minuteLabel(total: number) {
  const hour = Math.floor(total / 60);
  const minute = total % 60;
  return new Intl.DateTimeFormat("ar-EG", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZone: "UTC",
  }).format(new Date(Date.UTC(2026, 0, 1, hour, minute)));
}

export function QuickAppointmentDialog({
  branchId,
  bookingDate,
  column,
  windowStartMinutes,
  windowEndMinutes,
  phone,
  phoneInvalid,
  patient,
  history,
  packages,
  pulseBalances,
  devicePrices,
  fixedLaserDeviceKey,
  allowedOperationalCategory,
  services,
  doctors,
  staff,
  visibleColumns,
  closeHref,
  timezone,
}: {
  branchId: string;
  bookingDate: string;
  column: ScheduleColumnId;
  windowStartMinutes: number;
  windowEndMinutes: number;
  phone: string;
  phoneInvalid: boolean;
  patient: Patient | null;
  history: Appointment[];
  packages: PatientPackage[];
  pulseBalances: PulseBalance[];
  devicePrices: DevicePrice[];
  fixedLaserDeviceKey?: string;
  allowedOperationalCategory?: "laser" | "dermatology" | "slimming";
  services: Service[];
  doctors: Doctor[];
  staff: Staff[];
  visibleColumns: ScheduleColumnId[];
  closeHref: string;
  timezone: string;
}) {
  const serviceNames = new Map(services.map((service) => [service.id, service.name]));

  return (
    <div className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4" role="dialog" aria-modal="true" aria-label="إضافة موعد">
      <Link href={closeHref} aria-label="إغلاق" className="absolute inset-0 bg-slate-950/35 backdrop-blur-[1px]" />
      <section className="relative z-10 max-h-[92vh] w-full overflow-y-auto rounded-t-3xl border border-slate-200 bg-white shadow-2xl sm:max-w-3xl sm:rounded-3xl">
        <header className="sticky top-0 z-20 flex items-start justify-between gap-3 border-b border-slate-100 bg-white/95 px-4 py-4 backdrop-blur sm:px-6">
          <div>
            <h2 className="text-base font-black text-slate-950">{column === "quick" ? "حجز سريع استثنائي" : "إضافة موعد"}</h2>
            <p className="mt-1 text-xs font-semibold text-[var(--muted)]">
              {bookingDate} · الفترة {minuteLabel(windowStartMinutes)} – {minuteLabel(windowEndMinutes)}
            </p>
            {column === "quick" && (
              <p className="mt-1.5 max-w-xl text-[11px] font-semibold text-amber-700">
                الحجز السريع يسمح بتسجيل الموعد حتى مع وجود تداخل زمني مقصود. قيود العميل والخدمة والباكدج تظل مطبقة.
              </p>
            )}
          </div>
          <Link href={closeHref} className="grid size-9 place-items-center rounded-full border border-slate-200 text-slate-600 hover:bg-slate-50" aria-label="إغلاق">
            <X size={17} />
          </Link>
        </header>

        <div className="space-y-5 p-4 sm:p-6">
          <form method="get" className="flex gap-2">
            <input type="hidden" name="date" value={bookingDate} />
            <input type="hidden" name="branch_id" value={branchId} />
            <input type="hidden" name="quick_column" value={column} />
            <input type="hidden" name="quick_start" value={windowStartMinutes} />
            <input type="hidden" name="quick_end" value={windowEndMinutes} />
            {visibleColumns.map((visibleColumn) => <input key={visibleColumn} type="hidden" name="column" value={visibleColumn} />)}
            <label className="min-w-0 flex-1">
              <span className="mb-1.5 block text-xs font-bold text-slate-600">رقم هاتف العميل</span>
              <Input
                name="quick_phone"
                defaultValue={phone}
                required
                inputMode="numeric"
                pattern="[0-9]{11}"
                minLength={11}
                maxLength={11}
                title={PATIENT_PHONE_ERROR}
                dir="ltr"
                placeholder="01xxxxxxxxx"
                autoFocus={!phone}
              />
              {phoneInvalid && <p className="mt-1 text-xs font-bold text-red-600">{PATIENT_PHONE_ERROR}</p>}
            </label>
            <Button type="submit" variant="outline" className="mt-6"><Search size={16} /> بحث</Button>
          </form>

          {phone && !phoneInvalid && (
            <>
              {patient && (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-black text-slate-950">الحجوزات السابقة لـ {patient.first_name} {patient.last_name || ""}</div>
                      <div className="mt-1 text-xs text-[var(--muted)]">آخر {Math.min(history.length, 20).toLocaleString("ar-EG")} موعد مسجل لهذا الرقم.</div>
                    </div>
                    <Link href={`/patients/${patient.id}`} className="text-xs font-bold text-[var(--accent-strong)] hover:underline">فتح ملف العميل</Link>
                  </div>
                  {history.length ? (
                    <div className="mt-3 grid gap-2 md:grid-cols-2">
                      {history.slice(0, 4).map((appointment) => (
                        <Link href={`/appointments/${appointment.id}`} key={appointment.id} className="rounded-xl bg-white px-3 py-2 text-xs transition hover:ring-1 hover:ring-[var(--accent-ring)]">
                          <div className="font-black text-slate-900">{serviceNames.get(appointment.service_id) || "خدمة"}</div>
                          <div className="mt-1 text-[var(--accent-strong)]">{formatDateTime(appointment.start_at)}</div>
                          <div className="mt-1 font-bold text-slate-700">{appointmentLabels[appointment.status] || appointment.status}</div>
                        </Link>
                      ))}
                    </div>
                  ) : <div className="mt-3 text-xs text-[var(--muted)]">لا توجد حجوزات سابقة لهذا العميل.</div>}
                </div>
              )}

              <ManualAppointmentForm
                mode={patient ? "existing" : "new"}
                phone={phone}
                bookingDate={bookingDate}
                branchId={branchId}
                patientId={patient?.id}
                patientName={patient ? `${patient.first_name} ${patient.last_name || ""}`.trim() : undefined}
                services={services}
                doctors={doctors}
                staff={staff}
                packages={packages}
                pulseBalances={pulseBalances}
                devicePrices={devicePrices}
                fixedLaserDeviceKey={column === "quick" ? undefined : fixedLaserDeviceKey}
                allowedOperationalCategory={column === "quick" ? undefined : allowedOperationalCategory}
                windowStartMinutes={windowStartMinutes}
                windowEndMinutes={windowEndMinutes}
                timezone={timezone}
                schedulingMode={column === "quick" ? "quick" : "standard"}
                successHref={closeHref}
              />
            </>
          )}
        </div>
      </section>
    </div>
  );
}
