"use server";

import { randomUUID } from "node:crypto";
import { revalidatePath } from "next/cache";
import { TiaApiError, tiaRequest } from "@/lib/tia/api";
import type { Appointment, Patient } from "@/lib/types";

export type ManualAppointmentState = { ok: boolean; message: string };
export type AvailabilityBlockActionState = { ok: boolean; message: string };

export async function createAvailabilityBlock(previous: AvailabilityBlockActionState, formData: FormData): Promise<AvailabilityBlockActionState> {
  void previous;
  const branchId = String(formData.get("branch_id") || "").trim();
  const date = String(formData.get("date") || "").trim();
  const startTime = String(formData.get("start_time") || "").trim();
  const endTime = String(formData.get("end_time") || "").trim();
  const reason = String(formData.get("reason") || "").trim();
  const scope = String(formData.get("scope") || "all_services").trim();
  const serviceIds = formData.getAll("service_ids").map((value) => String(value).trim()).filter(Boolean);
  const halfHour = /^(?:[01]\d|2[0-3]):(?:00|30)$/;
  if (!branchId || !/^\d{4}-\d{2}-\d{2}$/.test(date) || !halfHour.test(startTime) || !halfHour.test(endTime)) {
    return { ok: false, message: "اختر التاريخ ووقت البداية والنهاية بنصف ساعة." };
  }
  if (scope !== "all_services" && scope !== "selected_services") return { ok: false, message: "اختر نطاق قفل صحيح." };
  if (scope === "selected_services" && serviceIds.length === 0) return { ok: false, message: "اختر خدمة واحدة على الأقل." };
  try {
    const result = await tiaRequest<{ overlapping_appointments: number }>("/booking/availability-blocks", {
      method: "POST",
      body: JSON.stringify({ branch_id: branchId, date, start_time: startTime, end_time: endTime, scope, service_ids: scope === "selected_services" ? serviceIds : [], reason: reason || null }),
    });
    revalidatePath("/appointments");
    return {
      ok: true,
      message: result.overlapping_appointments > 0
        ? `تم قفل الفترة. فيها ${result.overlapping_appointments.toLocaleString("ar-EG")} موعد موجود بالفعل؛ المواعيد الحالية لم تتغير.`
        : "تم قفل الفترة للحجوزات العادية والـAI.",
    };
  } catch (error) {
    if (error instanceof TiaApiError && error.status === 409) {
      return { ok: false, message: "يوجد قفل مطابق لنفس الفترة والنطاق بالفعل." };
    }
    return { ok: false, message: error instanceof Error ? error.message : "تعذر قفل الفترة." };
  }
}

export async function reopenAvailabilityBlock(formData: FormData) {
  const blockId = String(formData.get("block_id") || "").trim();
  if (!blockId) return;
  await tiaRequest(`/booking/availability-blocks/${blockId}`, { method: "DELETE" });
  revalidatePath("/appointments");
}

export type ManualAvailabilitySlot = {
  doctor_id: string;
  start_at: string;
  end_at: string;
  laser_device_key?: string | null;
  laser_device_name?: string | null;
};

export type ManualAvailabilityResult = {
  ok: boolean;
  message: string;
  timezone: string;
  slots: ManualAvailabilitySlot[];
};

type AvailabilityResponse = {
  timezone: string;
  slots: ManualAvailabilitySlot[];
};

function minuteInTimezone(value: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return Number(values.hour) * 60 + Number(values.minute);
}

export async function getManualAppointmentAvailability(input: {
  branchId: string;
  serviceId: string;
  date: string;
  laserDeviceKey?: string;
  windowStartMinutes?: number;
  windowEndMinutes?: number;
}): Promise<ManualAvailabilityResult> {
  const branchId = input.branchId.trim();
  const serviceId = input.serviceId.trim();
  const date = input.date.trim();
  if (!branchId || !serviceId || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return { ok: false, message: "بيانات اليوم أو الخدمة غير مكتملة.", timezone: "Africa/Cairo", slots: [] };
  }

  try {
    const query = new URLSearchParams({
      branch_id: branchId,
      service_id: serviceId,
      date,
      allow_immediate: "true",
    });
    if (input.laserDeviceKey) query.set("laser_device_key", input.laserDeviceKey);
    const response = await tiaRequest<AvailabilityResponse>(`/booking/availability?${query.toString()}`);
    const hasWindow =
      Number.isFinite(input.windowStartMinutes) &&
      Number.isFinite(input.windowEndMinutes) &&
      Number(input.windowEndMinutes) > Number(input.windowStartMinutes);
    const slots = hasWindow
      ? response.slots.filter((slot) => {
          const minute = minuteInTimezone(slot.start_at, response.timezone);
          return minute >= Number(input.windowStartMinutes) && minute < Number(input.windowEndMinutes);
        })
      : response.slots;
    return {
      ok: true,
      message: slots.length ? "" : "مفيش مواعيد متاحة للخدمة دي في الفترة المختارة.",
      timezone: response.timezone,
      slots,
    };
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : "تعذر تحميل المواعيد المتاحة.",
      timezone: "Africa/Cairo",
      slots: [],
    };
  }
}

function appointmentErrorMessage(error: unknown, bookingMode: "quick" | "standard") {
  if (!(error instanceof TiaApiError)) {
    return error instanceof Error ? error.message : "تعذر تسجيل الموعد.";
  }

  const detail = (error.technicalMessage || "").toLowerCase();
  if (detail.includes("blocked patients")) {
    return "لا يمكن إضافة موعد لهذا العميل لأن ملفه محظور.";
  }
  if (detail.includes("belongs to another patient")) {
    return "الباكدج المختارة تخص عميلًا آخر.";
  }
  if (detail.includes("different service")) {
    return "الباكدج المختارة لا تخص الخدمة المحددة.";
  }
  if (detail.includes("different laser device") || detail.includes("does not match the appointment device")) {
    return "الباكدج المختارة مرتبطة بجهاز ليزر مختلف.";
  }
  if (detail.includes("package is not active")) {
    return "الباكدج المختارة غير نشطة في تاريخ الموعد.";
  }
  if (detail.includes("before its purchase")) {
    return "لا يمكن استخدام الباكدج في موعد يسبق تاريخ شرائها.";
  }
  if (detail.includes("session(s) remaining")) {
    return "لا توجد جلسات متبقية كافية في الباكدج المختارة.";
  }
  if (detail.includes("laser device choice is required")) {
    return "اختار جهاز الليزر المطلوب للخدمة.";
  }
  if (detail.includes("laser device cannot be selected")) {
    return "الخدمة المختارة لا تستخدم جهاز ليزر.";
  }
  if (detail.includes("no available pulse balance")) {
    return "لا يوجد رصيد Pulses متاح للعميل على الجهاز المختار.";
  }
  if (detail.includes("either a session package or pulse balance")) {
    return "اختار طريقة حساب واحدة فقط: باكيدج جلسات أو رصيد Pulses.";
  }
  if (detail.includes("scheduling override could not be applied")) {
    return "تعذر تطبيق تجاوز التعارض الزمني للحجز السريع بسبب إعداد غير متوقع في قاعدة البيانات. لم يتم إنشاء الموعد.";
  }
  if (detail.includes("only available to authenticated clinic staff")) {
    return "الحجز السريع متاح فقط لموظفي العيادة المسجلين.";
  }
  if (detail.includes("already submitted")) {
    return "تم إرسال طلب الحجز السريع بالفعل. حدّث الجدول للتأكد من الموعد.";
  }
  if (detail.includes("violates a database constraint")) {
    return "تعذر إنشاء الحجز السريع بسبب قيد في بيانات الموعد، وليس بسبب تداخل الوقت. راجع الخدمة والجهاز والباكدج.";
  }

  if (bookingMode === "quick" && error.status === 409) {
    return "تعذر إنشاء الحجز السريع بسبب قيد في بيانات الموعد. التداخل الزمني مسموح في الحجز السريع؛ راجع العميل والخدمة والجهاز أو الباكدج.";
  }
  if (error.status === 409) {
    return "الوقت أو المورد المطلوب غير متاح، أو توجد مشكلة في بيانات الحجز. راجع الاختيارات وحاول مرة أخرى.";
  }
  return error.message;
}

function refreshAppointmentViews(appointmentId: string, patientId?: string) {
  revalidatePath("/appointments");
  revalidatePath(`/appointments/${appointmentId}`);
  if (patientId) revalidatePath(`/patients/${patientId}`);
  revalidatePath("/patients");
  revalidatePath("/dashboard");
  revalidatePath("/analytics");
}

function manualStartToIso(value: string, timezone: string) {
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) {
    return localToIso(value, timezone);
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw new Error("اختار وقت صحيح من المواعيد المتاحة.");
  return parsed.toISOString();
}

function localToIso(value: string, timezone: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new Error("اختار تاريخ ووقت صحيحين.");
  const [, year, month, day, hour, minute] = match;
  const guess = Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute));
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone || "Africa/Cairo", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const parts = Object.fromEntries(formatter.formatToParts(new Date(guess)).map((part) => [part.type, part.value]));
  const renderedAsUtc = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second));
  return new Date(guess - (renderedAsUtc - guess)).toISOString();
}

export async function createManualAppointment(previous: ManualAppointmentState, formData: FormData): Promise<ManualAppointmentState> {
  void previous;
  const bookingMode = String(formData.get("booking_mode") || "standard") === "quick" ? "quick" : "standard";
  try {
    const mode = String(formData.get("customer_mode") || "existing");
    let patientId = String(formData.get("patient_id") || "").trim();
    if (mode === "new") {
      const firstName = String(formData.get("first_name") || "").trim();
      const lastName = String(formData.get("last_name") || "").trim();
      const phone = String(formData.get("phone") || "").trim();
      if (!firstName || !phone) return { ok: false, message: "اكتب اسم العميل ورقم الهاتف." };
      const patient = await tiaRequest<Patient>("/crm/patients", {
        method: "POST",
        body: JSON.stringify({ first_name: firstName, last_name: lastName || null, phone, source: "phone", status: "active", preferred_language: "ar" }),
      });
      patientId = patient.id;
    }

    const branchId = String(formData.get("branch_id") || "").trim();
    const clinicTimezone = String(formData.get("clinic_timezone") || "Africa/Cairo").trim() || "Africa/Cairo";
    const doctorId = String(formData.get("doctor_id") || "").trim();
    const serviceId = String(formData.get("service_id") || "").trim();
    const startsAt = String(formData.get("start_at") || "").trim();
    const packageId = String(formData.get("patient_package_id") || "").trim();
    const usePulseBalance = String(formData.get("use_pulse_balance") || "false") === "true";
    const laserDeviceKey = String(formData.get("laser_device_key") || "").trim();
    if (!patientId || !branchId || !doctorId || !serviceId || !startsAt) return { ok: false, message: "كمّل بيانات العميل والخدمة والدكتور والموعد." };

    const endpoint = bookingMode === "quick" ? "/booking/appointments/quick" : "/booking/appointments";
    const appointment = await tiaRequest<Appointment>(endpoint, {
      method: "POST",
      headers: { "Idempotency-Key": `dashboard-${bookingMode}-appointment:${randomUUID()}` },
      body: JSON.stringify({
        patient_id: patientId,
        branch_id: branchId,
        doctor_id: doctorId,
        service_id: serviceId,
        patient_package_id: packageId || null,
        use_pulse_balance: usePulseBalance,
        laser_device_key: laserDeviceKey || null,
        start_at: manualStartToIso(startsAt, clinicTimezone),
        ...(bookingMode === "standard" ? { source: "staff" } : {}),
      }),
    });
    refreshAppointmentViews(appointment.id, patientId);
    return { ok: true, message: "تم تسجيل الموعد بنجاح." };
  } catch (error) {
    return { ok: false, message: appointmentErrorMessage(error, bookingMode) };
  }
}

export async function changeAppointmentStatus(formData: FormData) {
  const appointmentId = String(formData.get("appointment_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const status = String(formData.get("status") || "");
  const canOverrideCancellation = formData.get("can_override_cancellation") === "1";
  if (!appointmentId || !status) return;
  if (status === "confirmed") await tiaRequest(`/booking/appointments/${appointmentId}/confirm`, { method: "POST" });
  else if (status === "completed" || status === "no_show") await tiaRequest(`/booking/appointments/${appointmentId}/status`, { method: "POST", body: JSON.stringify({ status, reason: "manual_status_change_from_appointments" }) });
  else if (status === "cancelled") await tiaRequest(`/booking/appointments/${appointmentId}/cancel`, { method: "POST", body: JSON.stringify({ reason: "تم إلغاء الموعد يدويًا من لوحة المواعيد", override_policy: canOverrideCancellation }) });
  else return;
  refreshAppointmentViews(appointmentId, patientId || undefined);
}
