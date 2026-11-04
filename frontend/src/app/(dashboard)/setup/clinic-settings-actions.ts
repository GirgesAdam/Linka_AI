"use server";

import { revalidatePath } from "next/cache";

import type { ClinicKnowledgeText } from "@/lib/clinic-knowledge-base-types";
import type { ClinicHour, ClinicSetupV2Snapshot } from "@/lib/clinic-setup-v2-types";
import type { ClinicLaserDevice } from "@/lib/types";
import { actionFeedbackFailure, type ActionFeedbackState } from "@/lib/action-feedback";
import { tiaRequest } from "@/lib/tia/api";

function refresh() {
  revalidatePath("/setup");
  revalidatePath("/dashboard");
}

function text(formData: FormData, key: string) {
  const value = String(formData.get(key) ?? "").trim();
  return value || null;
}

function success(notice: string): ActionFeedbackState {
  return { notice, error: null };
}

export async function saveClinicProfileFormAction(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const name = text(formData, "name");
    if (!name) throw new Error("اسم العيادة مطلوب.");
    await tiaRequest<ClinicSetupV2Snapshot>("/clinic/setup-v2/profile", {
      method: "PUT",
      body: JSON.stringify({
        name,
        phone: text(formData, "phone"),
        address: text(formData, "address"),
        city: text(formData, "city"),
      }),
    });
    refresh();
    return success("تم حفظ بيانات العيادة بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر حفظ بيانات العيادة. حاول مرة أخرى.");
  }
}

export async function saveClinicHoursFormAction(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const intervals: ClinicHour[] = [];
    for (let weekday = 0; weekday < 7; weekday += 1) {
      if (formData.get("enabled_" + weekday) !== "on") continue;
      const start = text(formData, "start_" + weekday);
      const end = text(formData, "end_" + weekday);
      if (!start || !end) {
        throw new Error("حدد بداية ونهاية مواعيد كل يوم مفعّل.");
      }
      intervals.push({ weekday, start_time: start, end_time: end });
    }
    await tiaRequest<ClinicSetupV2Snapshot>("/clinic/setup-v2/hours", {
      method: "PUT",
      body: JSON.stringify({ intervals }),
    });
    refresh();
    return success("تم حفظ مواعيد العمل بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر حفظ مواعيد العمل. حاول مرة أخرى.");
  }
}

export async function addLaserDeviceFormAction(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const name = text(formData, "name");
    if (!name) throw new Error("اكتب اسم الجهاز.");
    await tiaRequest<ClinicLaserDevice>("/inventory/laser-devices", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    revalidatePath("/setup");
    revalidatePath("/services");
    revalidatePath("/appointments");
    return success("تمت إضافة الجهاز بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر إضافة الجهاز. حاول مرة أخرى.");
  }
}

export async function updateLaserDeviceFormAction(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const deviceId = String(formData.get("device_id") || "").trim();
    if (!deviceId) throw new Error("تعذر تحديد الجهاز المطلوب تعديله.");
    const name = text(formData, "name");
    await tiaRequest<ClinicLaserDevice>("/inventory/laser-devices/" + deviceId, {
      method: "PATCH",
      body: JSON.stringify({
        name,
        is_active: formData.get("is_active") === "on",
      }),
    });
    revalidatePath("/setup");
    revalidatePath("/services");
    revalidatePath("/appointments");
    return success("تم حفظ بيانات الجهاز بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر حفظ بيانات الجهاز. حاول مرة أخرى.");
  }
}

export async function saveKnowledgeTextFormAction(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const content = String(formData.get("content") ?? "").trim();
    await tiaRequest<ClinicKnowledgeText>("/clinic/knowledge-text", {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
    refresh();
    return success("تم حفظ معلومات Linka بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر حفظ معلومات Linka. حاول مرة أخرى.");
  }
}