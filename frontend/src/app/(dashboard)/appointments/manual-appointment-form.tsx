"use client";

import { useActionState, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import type { Doctor, PatientPackage, PulseBalance, Service, Staff } from "@/lib/types";
import {
  createManualAppointment,
  getManualAppointmentAvailability,
  type ManualAppointmentState,
  type ManualAvailabilitySlot,
} from "./actions";

const initialState: ManualAppointmentState = { ok: false, message: "" };
type PricedService = Service & { requires_laser_device?: boolean };
type DevicePackage = PatientPackage & {
  laser_device_key?: string | null;
  laser_device_name?: string | null;
};
type DevicePrice = {
  service_id: string;
  device_key: string;
  device_name: string;
  price_minor: number | null;
  duration_minutes: number | null;
  currency: string;
  configured: boolean;
};

function timeLabel(value: string, timezone: string) {
  return new Intl.DateTimeFormat("ar-EG", {
    timeZone: timezone,
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(value));
}

export function ManualAppointmentForm({
  mode,
  phone,
  bookingDate,
  branchId,
  patientId,
  patientName,
  services,
  doctors,
  staff,
  packages = [],
  pulseBalances = [],
  devicePrices = [],
  fixedLaserDeviceKey,
  allowedOperationalCategory,
  windowStartMinutes,
  windowEndMinutes,
  timezone,
  schedulingMode = "standard",
  successHref,
}: {
  mode: "existing" | "new";
  phone: string;
  bookingDate: string;
  branchId: string;
  patientId?: string;
  patientName?: string;
  services: PricedService[];
  doctors: Doctor[];
  staff: Staff[];
  packages?: DevicePackage[];
  pulseBalances?: PulseBalance[];
  devicePrices?: DevicePrice[];
  fixedLaserDeviceKey?: string;
  allowedOperationalCategory?: "laser" | "dermatology" | "slimming";
  windowStartMinutes?: number;
  windowEndMinutes?: number;
  timezone: string;
  schedulingMode?: "standard" | "quick";
  successHref?: string;
}) {
  const router = useRouter();
  const [state, formAction, pending] = useActionState(createManualAppointment, initialState);
  const [serviceId, setServiceId] = useState("");
  const [laserDeviceKey, setLaserDeviceKey] = useState(fixedLaserDeviceKey || "");
  const [packageId, setPackageId] = useState("");
  const [usePulseBalance, setUsePulseBalance] = useState(false);
  const [startAt, setStartAt] = useState("");
  const [doctorId, setDoctorId] = useState("");
  const [slots, setSlots] = useState<ManualAvailabilitySlot[]>([]);
  const [availabilityTimezone, setAvailabilityTimezone] = useState("Africa/Cairo");
  const [availabilityLoading, setAvailabilityLoading] = useState(false);
  const [availabilityMessage, setAvailabilityMessage] = useState("");
  const [quickTime, setQuickTime] = useState("");

  useEffect(() => {
    if (!state.ok || !successHref) return;
    router.replace(successHref);
    router.refresh();
  }, [router, state.ok, successHref]);

  const staffMap = useMemo(
    () => new Map(staff.map((item) => [item.id, `${item.first_name} ${item.last_name}`.trim()])),
    [staff],
  );
  const selectedService = services.find((item) => item.id === serviceId);
  const requiresLaserDevice = Boolean(selectedService?.requires_laser_device);
  const availableDevices = useMemo(
    () =>
      devicePrices.filter(
        (item) =>
          item.service_id === serviceId &&
          item.configured &&
          item.price_minor != null &&
          item.duration_minutes != null,
      ),
    [devicePrices, serviceId],
  );
  const fixedDeviceName =
    devicePrices.find(
      (item) =>
        item.service_id === serviceId &&
        item.device_key === fixedLaserDeviceKey,
    )?.device_name || fixedLaserDeviceKey;
  const visibleServices = useMemo(
    () =>
      services.filter((item) => {
        if (!item.is_active) return false;
        if (allowedOperationalCategory && item.operational_category !== allowedOperationalCategory) return false;
        if (fixedLaserDeviceKey && !item.requires_laser_device) return false;
        return true;
      }),
    [services, allowedOperationalCategory, fixedLaserDeviceKey],
  );
  const compatiblePackages = useMemo(
    () =>
      packages.filter((item) => {
        if (item.service_id !== serviceId) return false;
        if (!item.laser_device_key) return true;
        return requiresLaserDevice && Boolean(laserDeviceKey) && item.laser_device_key === laserDeviceKey;
      }),
    [packages, serviceId, requiresLaserDevice, laserDeviceKey],
  );
  const pulseBalance = useMemo(
    () =>
      requiresLaserDevice
        ? pulseBalances.find(
            (item) => item.device_key === laserDeviceKey && item.pulses_remaining > 0,
          ) || null
        : null,
    [pulseBalances, requiresLaserDevice, laserDeviceKey],
  );

  const timeOptions = useMemo(() => {
    const unique = new Map<string, ManualAvailabilitySlot>();
    slots.forEach((slot) => {
      if (!unique.has(slot.start_at)) unique.set(slot.start_at, slot);
    });
    return [...unique.values()].sort((a, b) => a.start_at.localeCompare(b.start_at));
  }, [slots]);

  const doctorOptions = useMemo(() => {
    if (schedulingMode === "quick") return doctors.filter((doctor) => doctor.is_active);
    const ids = new Set(slots.filter((slot) => slot.start_at === startAt).map((slot) => slot.doctor_id));
    return doctors.filter((doctor) => doctor.is_active && ids.has(doctor.id));
  }, [doctors, schedulingMode, slots, startAt]);

  async function loadAvailability(nextServiceId: string, nextDeviceKey: string) {
    if (schedulingMode === "quick") return;
    setStartAt("");
    setDoctorId("");
    setSlots([]);
    setAvailabilityMessage("");
    if (!nextServiceId) return;

    const service = services.find((item) => item.id === nextServiceId);
    if (!service) return;
    if (service.requires_laser_device && !nextDeviceKey) {
      setAvailabilityMessage("اختار جهاز الليزر الأول علشان نعرض المواعيد المتاحة.");
      return;
    }

    setAvailabilityLoading(true);
    const result = await getManualAppointmentAvailability({
      branchId,
      serviceId: nextServiceId,
      date: bookingDate,
      laserDeviceKey: nextDeviceKey || undefined,
      windowStartMinutes,
      windowEndMinutes,
    });
    setAvailabilityLoading(false);
    setAvailabilityTimezone(result.timezone);
    setSlots(result.slots);
    setAvailabilityMessage(result.message);
  }

  return (
    <form action={formAction} className="space-y-4">
      <input type="hidden" name="customer_mode" value={mode} />
      <input type="hidden" name="booking_mode" value={schedulingMode} />
      <input type="hidden" name="clinic_timezone" value={timezone} />
      <input type="hidden" name="branch_id" value={branchId} />
      <input type="hidden" name="start_at" value={startAt} />
      <input type="hidden" name="use_pulse_balance" value={usePulseBalance ? "true" : "false"} />
      {mode === "existing" && <input type="hidden" name="patient_id" value={patientId} />}

      {mode === "new" ? (
        <>
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm font-bold text-amber-900">
            الرقم غير مسجل. كمّل بيانات العميل لإنشاء ملفه مع الموعد.
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <label>
              <span className="mb-1.5 block text-xs font-bold text-slate-600">الاسم الأول</span>
              <Input name="first_name" required maxLength={120} placeholder="الاسم" />
            </label>
            <label>
              <span className="mb-1.5 block text-xs font-bold text-slate-600">اسم العائلة (اختياري)</span>
              <Input name="last_name" maxLength={120} placeholder="اسم العائلة" />
            </label>
            <label>
              <span className="mb-1.5 block text-xs font-bold text-slate-600">رقم الهاتف</span>
              <Input name="phone" required readOnly value={phone} dir="ltr" />
            </label>
          </div>
        </>
      ) : (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-3 text-sm text-emerald-950">
          <div className="font-black">العميل موجود: {patientName || "عميل"}</div>
          <div className="mt-1 font-semibold" dir="ltr">{phone}</div>
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2">
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">الخدمة</span>
          <Select
            name="service_id"
            required
            value={serviceId}
            onChange={(event) => {
              const nextServiceId = event.target.value;
              const service = services.find((item) => item.id === nextServiceId);
              const nextDeviceKey = service?.requires_laser_device ? (fixedLaserDeviceKey || "") : "";
              setServiceId(nextServiceId);
              setLaserDeviceKey(nextDeviceKey);
              setPackageId("");
              setUsePulseBalance(false);
              if (schedulingMode === "standard") void loadAvailability(nextServiceId, nextDeviceKey);
            }}
          >
            <option value="" disabled>اختار الخدمة</option>
            {visibleServices.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </Select>
        </label>

        {requiresLaserDevice && (
          <label>
            <span className="mb-1.5 block text-xs font-bold text-slate-600">جهاز الليزر</span>
            {fixedLaserDeviceKey ? (
              <>
                <input type="hidden" name="laser_device_key" value={fixedLaserDeviceKey} />
                <div className="form-control flex h-10 min-h-10 items-center bg-slate-50 text-sm font-bold text-slate-700">
                  {fixedDeviceName || "جهاز الليزر"}
                </div>
              </>
            ) : (
              <Select
                name="laser_device_key"
                required
                value={laserDeviceKey}
                onChange={(event) => {
                  const next = event.target.value;
                  setLaserDeviceKey(next);
                  setPackageId("");
                  setUsePulseBalance(false);
                  if (schedulingMode === "standard") void loadAvailability(serviceId, next);
                }}
              >
                <option value="" disabled>اختار الجهاز</option>
                {availableDevices.map((device) => (
                  <option key={device.device_key} value={device.device_key}>
                    {device.device_name}
                  </option>
                ))}
              </Select>
            )}
          </label>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">
            {schedulingMode === "quick" ? "وقت الحجز السريع" : "الميعاد المتاح"}
          </span>
          {schedulingMode === "quick" ? (
            <Input
              type="time"
              required
              step="60"
              value={quickTime}
              min={typeof windowStartMinutes === "number" ? `${String(Math.floor(windowStartMinutes / 60)).padStart(2, "0")}:${String(windowStartMinutes % 60).padStart(2, "0")}` : undefined}
              max={typeof windowEndMinutes === "number" ? `${String(Math.floor(windowEndMinutes / 60)).padStart(2, "0")}:${String(windowEndMinutes % 60).padStart(2, "0")}` : undefined}
              onChange={(event) => {
                const next = event.target.value;
                setQuickTime(next);
                setStartAt(next ? `${bookingDate}T${next}` : "");
              }}
            />
          ) : (
            <Select
              required
              value={startAt}
              disabled={!serviceId || availabilityLoading || timeOptions.length === 0}
              onChange={(event) => {
                setStartAt(event.target.value);
                setDoctorId("");
              }}
            >
              <option value="" disabled>{availabilityLoading ? "جارٍ تحميل المواعيد..." : "اختار الميعاد"}</option>
              {timeOptions.map((slot) => (
                <option key={slot.start_at} value={slot.start_at}>
                  {timeLabel(slot.start_at, availabilityTimezone)} – {timeLabel(slot.end_at, availabilityTimezone)}
                </option>
              ))}
            </Select>
          )}
          <span className="mt-1 block text-[11px] text-[var(--muted)]">
            {schedulingMode === "quick"
              ? "اختار وقت الموعد داخل ساعات العمل. الحجز السريع يسمح بالتداخل الزمني عند الحاجة، لكنه يظل يتحقق من العميل والخدمة والباكدج."
              : `بنعرض فقط الأوقات المسموح حجزها يوم ${bookingDate}.`}
          </span>
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-bold text-slate-600">
            {schedulingMode === "quick" ? "الدكتور" : "الدكتور المتاح في الميعاد"}
          </span>
          <Select
            name="doctor_id"
            required
            value={doctorId}
            disabled={(schedulingMode === "standard" && !startAt) || doctorOptions.length === 0}
            onChange={(event) => setDoctorId(event.target.value)}
          >
            <option value="" disabled>اختار الدكتور</option>
            {doctorOptions.map((doctor) => (
              <option key={doctor.id} value={doctor.id}>{staffMap.get(doctor.staff_id) || "دكتور"}</option>
            ))}
          </Select>
          <span className="mt-1 block text-[11px] text-[var(--muted)]">
            {schedulingMode === "quick"
              ? "اختار الدكتور المطلوب. القائمة تعرض الدكاترة النشطين، وليس معنى ظهور الدكتور أنه متاح في الوقت المختار."
              : "القائمة دي بتتحدد بعد اختيار الميعاد، وبتشمل المتاحين في الوقت ده فقط."}
          </span>
        </label>
      </div>

      {schedulingMode === "standard" && availabilityMessage && (
        <div className="rounded-xl bg-slate-50 px-3 py-2 text-xs font-bold text-slate-700">
          {availabilityMessage}
        </div>
      )}

      {mode === "existing" && serviceId && (compatiblePackages.length > 0 || pulseBalance) && (
        <div className="max-w-xl rounded-xl border border-slate-200 bg-slate-50/60 p-3">
          <div className="text-xs font-black text-slate-700">طريقة الحساب</div>
          <div className="mt-2 space-y-2">
            {pulseBalance && (
              <label className="flex cursor-pointer items-start gap-2 rounded-lg bg-white p-3 text-sm">
                <input
                  type="checkbox"
                  checked={usePulseBalance}
                  onChange={(event) => {
                    const checked = event.target.checked;
                    setUsePulseBalance(checked);
                    if (checked) setPackageId("");
                  }}
                />
                <span>
                  <b>استخدام رصيد Pulses</b>
                  <span className="mt-0.5 block text-xs text-[var(--muted)]">
                    متبقي {pulseBalance.pulses_remaining.toLocaleString("ar-EG")} Pulse على {pulseBalance.device_name}. الخصم الفعلي يتم بعد الجلسة.
                  </span>
                </span>
              </label>
            )}
            {compatiblePackages.length > 0 && (
              <label className="block">
                <span className="mb-1.5 block text-xs font-bold text-slate-600">أو باكيدج جلسات</span>
                <Select
                  name="patient_package_id"
                  value={packageId}
                  disabled={usePulseBalance}
                  onChange={(event) => {
                    setPackageId(event.target.value);
                    if (event.target.value) setUsePulseBalance(false);
                  }}
                >
                  <option value="">بدون باكدج جلسات</option>
                  {compatiblePackages.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name} · متبقي {item.sessions_remaining}
                      {item.laser_device_name ? ` · ${item.laser_device_name}` : ""}
                    </option>
                  ))}
                </Select>
              </label>
            )}
          </div>
        </div>
      )}

      {state.message && (
        <div className={`rounded-xl px-3 py-2 text-sm font-bold ${state.ok ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"}`}>
          {state.message}
        </div>
      )}
      <div className="flex justify-end">
        <Button type="submit" disabled={pending || availabilityLoading || !startAt || !doctorId}>
          {pending ? "جارٍ التسجيل..." : "تسجيل الموعد"}
        </Button>
      </div>
    </form>
  );
}
