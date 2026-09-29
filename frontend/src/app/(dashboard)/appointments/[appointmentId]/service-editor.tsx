"use client";

import { useActionState, useState } from "react";
import { CalendarClock, Save, Stethoscope } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatMoney } from "@/lib/format";

import { changeAppointmentService, type AppointmentServiceChangeState } from "./actions";

export type AppointmentServiceOption = {
  id: string;
  name: string;
  price_minor: number;
  currency: string;
  requires_laser_device: boolean;
  is_active: boolean;
};

export type AppointmentDevicePrice = {
  service_id: string;
  device_key: "prime_lase" | "candela_gentle";
  device_name: string;
  price_minor: number | null;
  currency: string;
  configured: boolean;
};

export type AppointmentDoctorOption = {
  id: string;
  name: string;
};

type Props = {
  appointmentId: string;
  patientId: string;
  currentServiceId: string;
  currentDoctorId: string;
  currentDeviceKey: string | null;
  packageBacked: boolean;
  services: AppointmentServiceOption[];
  doctors: AppointmentDoctorOption[];
  devicePrices: AppointmentDevicePrice[];
};

const initialState: AppointmentServiceChangeState = { ok: false, error: null };

export function AppointmentServiceEditor({
  appointmentId,
  patientId,
  currentServiceId,
  currentDoctorId,
  currentDeviceKey,
  packageBacked,
  services,
  doctors,
  devicePrices,
}: Props) {
  const [state, formAction, pending] = useActionState(changeAppointmentService, initialState);
  const [serviceId, setServiceId] = useState(currentServiceId);
  const [doctorId, setDoctorId] = useState(currentDoctorId);
  const [deviceKey, setDeviceKey] = useState(currentDeviceKey || "");
  const [startAt, setStartAt] = useState("");
  const selected = services.find((service) => service.id === serviceId) || null;
  const devices = devicePrices.filter(
    (row) => row.service_id === serviceId && row.configured && row.price_minor != null,
  );
  const selectedDevice = devices.find((row) => row.device_key === deviceKey) || null;
  const deviceReady = !selected?.requires_laser_device || Boolean(selectedDevice);
  const changed =
    serviceId !== currentServiceId ||
    doctorId !== currentDoctorId ||
    deviceKey !== (currentDeviceKey || "") ||
    Boolean(startAt);

  return (
    <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50/60 p-3">
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-black text-slate-900">
        <Stethoscope size={16} /> تعديل الموعد يدويًا
      </summary>
      <form action={formAction} className="mt-4 space-y-3">
        <input type="hidden" name="appointment_id" value={appointmentId} />
        <input type="hidden" name="patient_id" value={patientId} />
        <label className="block text-xs font-bold text-slate-700">
          الخدمة
          <select
            name="service_id"
            value={serviceId}
            onChange={(event) => {
              const nextServiceId = event.target.value;
              setServiceId(nextServiceId);
              setDeviceKey(nextServiceId === currentServiceId ? currentDeviceKey || "" : "");
            }}
            className="form-control mt-1.5 h-10 min-h-10"
          >
            {services.filter((service) => service.is_active).map((service) => (
              <option key={service.id} value={service.id}>{service.name}</option>
            ))}
          </select>
        </label>

        <label className="block text-xs font-bold text-slate-700">
          الدكتور
          <select
            name="doctor_id"
            value={doctorId}
            onChange={(event) => setDoctorId(event.target.value)}
            required
            className="form-control mt-1.5 h-10 min-h-10"
          >
            {doctors.map((doctor) => <option key={doctor.id} value={doctor.id}>{doctor.name}</option>)}
          </select>
          <span className="mt-1 block text-[11px] font-semibold text-slate-500">
            لو غيرت الدكتور، النظام هيفحص توافره مع الخدمة والجهاز في الوقت المختار قبل الحفظ.
          </span>
        </label>

        <label className="block text-xs font-bold text-slate-700">
          <span className="flex items-center gap-1.5"><CalendarClock size={14} /> وقت الموعد الجديد (اختياري)</span>
          <input
            type="datetime-local"
            name="start_at"
            value={startAt}
            onChange={(event) => setStartAt(event.target.value)}
            className="form-control mt-1.5 h-10 min-h-10"
          />
          <span className="mt-1 block text-[11px] font-semibold text-slate-500">
            سيبه فاضي لو عايز تحتفظ بنفس وقت الموعد الحالي.
          </span>
        </label>

        {selected?.requires_laser_device ? (
          <label className="block text-xs font-bold text-slate-700">
            جهاز الليزر
            <select
              name="laser_device_key"
              value={deviceKey}
              onChange={(event) => setDeviceKey(event.target.value)}
              required
              className="form-control mt-1.5 h-10 min-h-10"
            >
              <option value="" disabled>اختار الجهاز</option>
              {devices.map((row) => (
                <option key={row.device_key} value={row.device_key}>
                  {row.device_name} · {formatMoney(row.price_minor || 0, row.currency)}
                </option>
              ))}
            </select>
            {!devices.length && <span className="mt-1 block text-[11px] text-amber-700">لا يوجد سعر جهاز مفعّل لهذه الخدمة، لذلك لا يمكن نقل الموعد لها حاليًا.</span>}
          </label>
        ) : (
          <input type="hidden" name="laser_device_key" value="" />
        )}

        {selected && !selected.requires_laser_device && (
          <div className="rounded-lg bg-white px-3 py-2 text-xs text-slate-600">
            سعر الخدمة الحالي: <b>{formatMoney(selected.price_minor, selected.currency)}</b>
          </div>
        )}

        {packageBacked && serviceId !== currentServiceId && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs font-semibold text-amber-900">
            الموعد الحالي مرتبط بباكيدج للخدمة القديمة. عند حفظ خدمة مختلفة سترجع الجلسة المحجوزة للباكيدج القديمة، وسيصبح الموعد بالحساب العادي للخدمة الجديدة. لن يختار النظام باكيدج جديدة تلقائيًا.
          </div>
        )}
        <div className="rounded-xl border border-slate-200 bg-white p-3 text-xs text-slate-600">
          لو سيبت الوقت والدكتور زي ما هم، تعديل الخدمة أو الجهاز هيحافظ على نفس الحجز ويفحص التعارضات الفعلية فقط. لو غيرت الوقت أو الدكتور، هيتم فحص التوافر الكامل قبل الحفظ. المدفوعات المسجلة لن تُحذف؛ سيُعاد فقط حساب المتبقي على السعر الجديد.
        </div>
        {state.error && (
          <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-3 text-xs font-bold text-red-800">
            {state.error}
          </div>
        )}
        {state.ok && !state.error && (
          <div className="rounded-xl border border-[var(--accent-border)] bg-[var(--accent-soft)] p-3 text-xs font-bold text-[var(--accent-strong)]">
            تم حفظ تعديل الموعد بنجاح.
          </div>
        )}
        <Button type="submit" size="sm" disabled={!changed || !deviceReady || !doctorId || pending}>
          <Save size={14} /> {pending ? "جاري الحفظ..." : "حفظ تعديلات الموعد"}
        </Button>
      </form>
    </details>
  );
}
