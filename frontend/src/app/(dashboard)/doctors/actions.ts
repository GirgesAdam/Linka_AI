"use server";

import { revalidatePath } from "next/cache";

import { TiaApiError, tiaRequest } from "@/lib/tia/api";

export type DoctorAdminState = {
  notice: string | null;
  error: string | null;
  saved?: {
    doctor_id: string;
    name: string;
    booking_enabled: boolean;
  } | null;
};

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
  if (values.some((value) => !allowed.has(value))) throw new Error("تصنيف خدمات الدكتور غير صالح.");
  return values;
}

function parseIntervals(formData: FormData): WorkingHourInterval[] {
  const raw = String(formData.get("intervals_json") || "[]");
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error("مواعيد العمل غير صالحة. راجع الأيام والساعات وحاول مرة أخرى.");
  }
  if (!Array.isArray(value)) throw new Error("مواعيد العمل غير صالحة.");
  const intervals: WorkingHourInterval[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") throw new Error("مواعيد العمل غير صالحة.");
    const row = item as Record<string, unknown>;
    const weekday = Number(row.weekday);
    const startTime = String(row.start_time || "");
    const endTime = String(row.end_time || "");
    if (!Number.isInteger(weekday) || weekday < 0 || weekday > 6) throw new Error("يوم العمل غير صالح.");
    if (!/^\d{2}:\d{2}$/.test(startTime) || !/^\d{2}:\d{2}$/.test(endTime) || endTime <= startTime) {
      throw new Error("وقت نهاية كل فترة لازم يكون بعد وقت البداية.");
    }
    intervals.push({ weekday, start_time: startTime, end_time: endTime });
  }
  return intervals;
}

function stateFromError(error: unknown): DoctorAdminState {
  if (error instanceof TiaApiError) {
    return { notice: null, error: error.technicalMessage || error.message, saved: null };
  }
  if (error instanceof Error) return { notice: null, error: error.message, saved: null };
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
