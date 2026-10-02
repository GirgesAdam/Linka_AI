"use client";

import { useActionState, useRef, useState } from "react";
import { CalendarClock, Save, Stethoscope } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatMoney } from "@/lib/format";

import {
  changeAppointmentService,
  getAppointmentEditAvailability,
  type AppointmentEditAvailabilitySlot,
  type AppointmentServiceChangeState,
} from "./actions";

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
  device_key: string;
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
  branchId: string;
  currentStartAt: string;
  timezone: string;
  currentServiceId: string;
  currentDoctorId: string;
  currentDeviceKey: string | null;
  packageBacked: boolean;
  services: AppointmentServiceOption[];
  doctors: AppointmentDoctorOption[];
  devicePrices: AppointmentDevicePrice[];
};

const initialState: AppointmentServiceChangeState = { ok: false, error: null };

function localDateKey(value: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(value));
  const map = new Map(parts.map((part) => [part.type, part.value]));
  return `${map.get("year")}-${map.get("month")}-${map.get("day")}`;
}

function timeLabel(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(value));
}

export function AppointmentServiceEditor({
  appointmentId,
  patientId,
  branchId,
  currentStartAt,
  timezone,
  currentServiceId,
  currentDoctorId,
  currentDeviceKey,
  packageBacked,
  services,
  doctors,
  devicePrices,
}: Props) {
  const currentDate = localDateKey(currentStartAt, timezone);
  const [serviceId, setServiceId] = useState(currentServiceId);
  const [doctorId, setDoctorId] = useState(currentDoctorId);
  const [deviceKey, setDeviceKey] = useState(currentDeviceKey || "");
  const [editDate, setEditDate] = useState(currentDate);
  const [startAt, setStartAt] = useState("");
  const [slots, setSlots] = useState<AppointmentEditAvailabilitySlot[]>([]);
  const [availabilityTimezone, setAvailabilityTimezone] = useState(timezone);
  const [availabilityMessage, setAvailabilityMessage] = useState("");
  const [availabilityLoading, setAvailabilityLoading] = useState(false);
  const availabilityRequest = useRef(0);

  function invalidateAvailability() {
    availabilityRequest.current += 1;
    setStartAt("");
    setSlots([]);
    setAvailabilityMessage("");
    setAvailabilityLoading(false);
  }

  async function submitAppointmentEdit(
    previous: AppointmentServiceChangeState,
    formData: FormData,
  ): Promise<AppointmentServiceChangeState> {
    const result = await changeAppointmentService(previous, formData);
    if (result.error) {
      setServiceId(currentServiceId);
      setDoctorId(currentDoctorId);
      setDeviceKey(currentDeviceKey || "");
      invalidateAvailability();
    }
    return result;
  }

  const [state, formAction, pending] = useActionState(submitAppointmentEdit, initialState);
  const selected = services.find((service) => service.id === serviceId) || null;
  const devices = devicePrices.filter(
    (row) => row.service_id === serviceId && row.configured && row.price_minor != null,
  );
  const selectedDevice = devices.find((row) => row.device_key === deviceKey) || null;
  const deviceReady = !selected?.requires_laser_device || Boolean(selectedDevice);
  const timeOptions = [
    ...new Map(slots.map((slot) => [slot.start_at, slot])).values(),
  ].sort((a, b) => a.start_at.localeCompare(b.start_at));
  const changed =
    serviceId !== currentServiceId ||
    doctorId !== currentDoctorId ||
    deviceKey !== (currentDeviceKey || "") ||
    Boolean(startAt);

  async function loadAvailability() {
    invalidateAvailability();
    if (!serviceId || !doctorId) {
      setAvailabilityMessage("اختار الخدمة والدكتور الأول.");
      return;
    }
    if (selected?.requires_laser_device && !deviceKey) {
      setAvailabilityMessage("اختار جهاز الليزر الأول علشان نعرض المواعيد المتاحة.");
      return;
    }

    const requestId = availabilityRequest.current + 1;
    availabilityRequest.current = requestId;
    setAvailabilityLoading(true);
    const result = await getAppointmentEditAvailability({
      appointmentId,
      branchId,
      serviceId,
      doctorId,
      date: editDate,
      laserDeviceKey: deviceKey || undefined,
    });
    if (availabilityRequest.current !== requestId) return;
    setAvailabilityLoading(false);
    setAvailabilityTimezone(result.timezone);
    setSlots(result.slots);
    setAvailabilityMessage(result.message);
  }

  return (
    <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50/60 p-3">
      <summary className="flex cursor-pointer items-center gap-2 text-sm font-black text-slate-900">
        <Stethoscope size={16} /> تعديل الموعد
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
              invalidateAvailability();
            }}
            className="form-control mt-1.5 h-10 min-h-10"
          >
            {services.filter((service) => service.is_active || service.id === currentServiceId).map((service) => (
              <option key={service.id} value={service.id}>{service.name}</option>
            ))}
          </select>
        </label>

        <label className="block text-xs font-bold text-slate-700">
          الدكتور
          <select
            name="doctor_id"
            value={doctorId}
            onChange={(event) => {
              setDoctorId(event.target.value);
              invalidateAvailability();
            }}
            required
            className="form-control mt-1.5 h-10 min-h-10"
          >
            {doctors.map((doctor) => <option key={doctor.id} value={doctor.id}>{doctor.name}</option>)}
          </select>
          <span className="mt-1 block text-[11px] font-semibold text-slate-500">
            لو غيرت الدكتور، النظام هيفحص توافره مع الخدمة والجهاز في الوقت المختار قبل الحفظ.
          </span>
        </label>

        <div className="grid gap-3 rounded-xl border border-slate-200 bg-white p-3 md:grid-cols-[minmax(170px,220px)_auto] md:items-end">
          <label className="block text-xs font-bold text-slate-700">
            <span className="flex items-center gap-1.5"><CalendarClock size={14} /> يوم الموعد الجديد (اختياري)</span>
            <input
              type="date"
              value={editDate}
              onChange={(event) => {
                setEditDate(event.target.value);
                invalidateAvailability();
              }}
              className="form-control mt-1.5 h-10 min-h-10"
            />
          </label>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={!deviceReady || !doctorId || availabilityLoading}
            onClick={() => void loadAvailability()}
          >
            <CalendarClock size={14} />
            {availabilityLoading ? "جاري تحميل المواعيد..." : "عرض المواعيد المتاحة"}
          </Button>
        </div>

        <label className="block text-xs font-bold text-slate-700">
          وقت الموعد الجديد (اختياري)
          <select
            name="start_at"
            value={startAt}
            disabled={availabilityLoading || timeOptions.length === 0}
            onChange={(event) => setStartAt(event.target.value)}
            className="form-control mt-1.5 h-10 min-h-10"
          >
            <option value="">الاحتفاظ بنفس وقت الموعد الحالي</option>
            {timeOptions.map((slot) => (
              <option key={slot.start_at} value={slot.start_at}>
                من {timeLabel(slot.start_at, availabilityTimezone)} إلى {timeLabel(slot.end_at, availabilityTimezone)}
              </option>
            ))}
          </select>
          <span className="mt-1 block text-[11px] font-semibold text-slate-500">
            لو عايز تغير الوقت، اختار اليوم واعرض المواعيد المتاحة. كل اختيار بيظهر من بداية الجلسة لنهايتها، من غير كتابة الساعة والدقيقة يدويًا.
          </span>
        </label>

        {availabilityMessage && (
          <div className="rounded-lg bg-slate-100 px-3 py-2 text-xs font-bold text-slate-700">
            {availabilityMessage}
          </div>
        )}

        {selected?.requires_laser_device ? (
          <label className="block text-xs font-bold text-slate-700">
            جهاز الليزر
            <select
              name="laser_device_key"
              value={deviceKey}
              onChange={(event) => {
                setDeviceKey(event.target.value);
                invalidateAvailability();
              }}
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
