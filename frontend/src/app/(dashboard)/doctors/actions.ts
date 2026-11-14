"use server";

import { revalidatePath } from "next/cache";

import { TiaApiError, tiaRequest } from "@/lib/tia/api";
import type { ActionFeedbackState } from "@/lib/action-feedback";

export type DoctorAdminState = {
  notice: string | null;
  error: string | null;
  saved?: {
    doctor_id: string;
    name: string;
    booking_enabled: boolean;
  } | null;
};

class DoctorInputError extends Error {}

type WorkingHourInterval = {
  weekday: number;
  start_time: string;
  end_time: string;
};

function clean(value: FormDataEntryValue | null) {
  const normalized = String(value || "").trim();
  return normalized || null;
}

function serviceCategories(formData: FormData) {
  const values = [...new Set(
    formData.getAll("service_category").map((value) => String(value).trim()).filter(Boolean),
  )];
  const allowed = new Set(["laser", "dermatology", "slimming"]);
  if (values.some((value) => !allowed.has(value))) throw new DoctorInputError("تصنيف خدمات الدكتور غير صالح.");
  return values;
}

function parseIntervals(formData: FormData): WorkingHourInterval[] {
  const raw = String(formData.get("intervals_json") || "[]");
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new DoctorInputError("مواعيد العمل غير صالحة. راجع الأيام والساعات وحاول مرة أخرى.");
  }
  if (!Array.isArray(value)) throw new DoctorInputError("مواعيد العمل غير صالحة.");
  const intervals: WorkingHourInterval[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") throw new DoctorInputError("مواعيد العمل غير صالحة.");
    const row = item as Record<string, unknown>;
    const weekday = Number(row.weekday);
    const startTime = String(row.start_time || "");
    const endTime = String(row.end_time || "");
    if (!Number.isInteger(weekday) || weekday < 0 || weekday > 6) throw new DoctorInputError("يوم العمل غير صالح.");
    if (!/^\d{2}:\d{2}$/.test(startTime) || !/^\d{2}:\d{2}$/.test(endTime) || endTime <= startTime) {
      throw new DoctorInputError("وقت نهاية كل فترة لازم يكون بعد وقت البداية.");
    }
    intervals.push({ weekday, start_time: startTime, end_time: endTime });
  }
  return intervals;
}

function stateFromError(error: unknown): DoctorAdminState {
  if (error instanceof TiaApiError) {
    return { notice: null, error: error.message, saved: null };
  }
  if (error instanceof DoctorInputError) return { notice: null, error: error.message, saved: null };
  return { notice: null, error: "تعذر تنفيذ التعديل. حاول مرة أخرى.", saved: null };
}

function refreshDoctorRelatedViews() {
  revalidatePath("/appointments");
  revalidatePath("/dashboard");
  revalidatePath("/knowledge");
}

function refreshDoctorViews() {
  revalidatePath("/doctors");
  refreshDoctorRelatedViews();
}

export async function createDoctorAction(
  _previousState: DoctorAdminState,
  formData: FormData,
): Promise<DoctorAdminState> {
  try {
    await tiaRequest("/clinic/doctor-admin", {
      method: "POST",
      body: JSON.stringify({
        name: clean(formData.get("name")),
        service_categories: serviceCategories(formData),
        booking_enabled: true,
        working_hours: { intervals: parseIntervals(formData) },
      }),
    });
    refreshDoctorViews();
    return { notice: "تمت إضافة الدكتور ومواعيد عمله بنجاح.", error: null, saved: null };
  } catch (error) {
    return stateFromError(error);
  }
}

export async function updateDoctorAction(
  _previousState: DoctorAdminState,
  formData: FormData,
): Promise<DoctorAdminState> {
  try {
    const doctorId = clean(formData.get("doctor_id"));
    if (!doctorId) return { notice: null, error: "تعذر تحديد الدكتور.", saved: null };

    const name = clean(formData.get("name"));
    const bookingEnabled = formData.get("booking_enabled") === "on";

    await tiaRequest(`/clinic/doctor-admin/${doctorId}`, {
      method: "PATCH",
      body: JSON.stringify({
        name,
        service_categories: serviceCategories(formData),
        booking_enabled: bookingEnabled,
      }),
    });
    // Do not revalidate /doctors here: replacing the server-rendered management
    // subtree remounts the open editor. The submitted values are already the
    // committed values; related server views can still be invalidated safely.
    refreshDoctorRelatedViews();
    return {
      notice: "تم تحديث بيانات الدكتور وتصنيفات الخدمات المتاحة له.",
      error: null,
      saved: {
        doctor_id: doctorId,
        name: name || "",
        booking_enabled: bookingEnabled,
      },
    };
  } catch (error) {
    return stateFromError(error);
  }
}

export async function updateDoctorScheduleAction(
  _previousState: DoctorAdminState,
  formData: FormData,
): Promise<DoctorAdminState> {
  try {
    const doctorId = clean(formData.get("doctor_id"));
    if (!doctorId) return { notice: null, error: "تعذر تحديد الدكتور.", saved: null };
    await tiaRequest(`/clinic/doctor-admin/${doctorId}/working-hours`, {
      method: "PUT",
      body: JSON.stringify({ intervals: parseIntervals(formData) }),
    });
    // The editor refreshes after showing confirmation, without immediately remounting the open form.
    refreshDoctorRelatedViews();
    return { notice: "تم تحديث مواعيد عمل الدكتور.", error: null, saved: null };
  } catch (error) {
    return stateFromError(error);
  }
}

export async function removeDoctorAction(
  _previousState: DoctorAdminState,
  formData: FormData,
): Promise<DoctorAdminState> {
  try {
    const doctorId = clean(formData.get("doctor_id"));
    if (!doctorId) return { notice: null, error: "تعذر تحديد الدكتور.", saved: null };
    await tiaRequest(`/clinic/doctor-admin/${doctorId}`, { method: "DELETE" });
    refreshDoctorViews();
    return { notice: "تمت إزالة الدكتور من الحجز النشط مع الاحتفاظ بالسجل التاريخي.", error: null, saved: null };
  } catch (error) {
    return stateFromError(error);
  }
}

export async function saveDoctorLaserWeekday(
  _previousState: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const doctorId = String(formData.get("doctor_id") || "");
    const branchId = String(formData.get("branch_id") || "");
    const rawWeekday = String(formData.get("weekday") ?? "");
    const weekday = /^\d$/.test(rawWeekday) ? Number(rawWeekday) : NaN;
    if (!doctorId || !branchId || !Number.isInteger(weekday) || weekday < 0 || weekday > 6) {
      throw new DoctorInputError("بيانات الدكتور أو اليوم غير صحيحة.");
    }
    const devices = formData.getAll("device_key").map(String);
    const allowed = new Set(formData.getAll("allowed_device_key").map(String));
    if (devices.length !== new Set(devices).size || [...allowed].some((key) => !devices.includes(key))) {
      throw new DoctorInputError("اختيارات الأجهزة غير صحيحة.");
    }

    await tiaRequest(`/clinic/doctor-laser-weekdays/${doctorId}/${branchId}/${weekday}`, {
      method: "PUT",
      body: JSON.stringify({ excluded_device_keys: devices.filter((key) => !allowed.has(key)) }),
    });
    revalidatePath("/doctors");
    return { notice: "تم حفظ أجهزة الليزر المسموحة لهذا اليوم.", error: null };
  } catch (error) {
    if (error instanceof TiaApiError) return { notice: null, error: error.message };
    if (error instanceof DoctorInputError) return { notice: null, error: error.message };
    return { notice: null, error: "تعذر حفظ أجهزة الدكتور. حاول مرة أخرى." };
  }
}
