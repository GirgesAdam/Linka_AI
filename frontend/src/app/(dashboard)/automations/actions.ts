"use server";

import { revalidatePath } from "next/cache";
import { TiaApiError, tiaRequest } from "@/lib/tia/api";

export type WhatsAppSetupActionState = {
  ok: boolean;
  message: string | null;
};

function safeTechnicalDetail(value: string | undefined) {
  const detail = value?.trim();
  if (!detail) return null;
  return detail
    .replace(/EA[A-Za-z0-9_-]{20,}/g, "[token hidden]")
    .replace(/\b\d{8,}\|[A-Za-z0-9_-]{16,}\b/g, "[app token hidden]")
    .slice(0, 700);
}

function actionErrorMessage(error: unknown) {
  if (error instanceof TiaApiError) {
    const technicalDetail = safeTechnicalDetail(error.technicalMessage);
    if (technicalDetail) {
      if (error.status === 502) return `Meta رفضت التحقق من بيانات الربط: ${technicalDetail}`;
      if (error.status === 409) return technicalDetail;
      if (error.status === 400 || error.status === 422) return technicalDetail;
      if (error.status === 503) return `إعداد الربط غير مكتمل: ${technicalDetail}`;
    }
  }
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/^Error:\s*/, "") || "تعذر إكمال الخطوة. حاول مرة أخرى.";
}

export async function connectWhatsappDirectAction(
  previous: WhatsAppSetupActionState,
  formData: FormData,
): Promise<WhatsAppSetupActionState> {
  void previous;
  const payload = {
    app_id: String(formData.get("app_id") || "").trim(),
    waba_id: String(formData.get("waba_id") || "").trim(),
    phone_number_id: String(formData.get("phone_number_id") || "").trim(),
    access_token: String(formData.get("access_token") || "").trim(),
    app_secret: String(formData.get("app_secret") || "").trim(),
  };

  if (!payload.app_id || !payload.waba_id || !payload.phone_number_id || !payload.access_token || !payload.app_secret) {
    return { ok: false, message: "كمّل الخانات الخمسة قبل التحقق والربط." };
  }

  try {
    await tiaRequest("/channels/whatsapp/setup/direct", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    revalidatePath("/automations");
    revalidatePath("/setup");
    return {
      ok: true,
      message: "بيانات Meta صحيحة واتخزنت بأمان. كمّل خطوة الـWebhook الظاهرة في الصفحة.",
    };
  } catch (error) {
    return { ok: false, message: actionErrorMessage(error) };
  }
}

export async function finishWhatsappDirectSetupAction(
  previous: WhatsAppSetupActionState,
  formData: FormData,
): Promise<WhatsAppSetupActionState> {
  void previous;
  void formData;
  try {
    await tiaRequest("/channels/whatsapp/setup/direct/finish", { method: "POST" });
    revalidatePath("/automations");
    revalidatePath("/setup");
    return {
      ok: true,
      message: "Linka تحققت من الـWebhook وبدأت فحص الرقم والقوالب ومسار الإرسال.",
    };
  } catch (error) {
    return { ok: false, message: actionErrorMessage(error) };
  }
}

export async function toggleAutomation(formData: FormData) {
  const id = String(formData.get("rule_id"));
  const enabled = String(formData.get("enabled")) === "true";
  await tiaRequest(`/automations/rules/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
  revalidatePath("/automations");
}

export async function saveAutomationTiming(formData: FormData) {
  const id = String(formData.get("rule_id") || "").trim();
  const triggerKind = String(formData.get("trigger_kind") || "").trim();
  const value = Number(formData.get("timing_value"));
  const unit = String(formData.get("timing_unit") || "hours");

  const multipliers: Record<string, number> = {
    minutes: 1,
    hours: 60,
    days: 1440,
  };
  const multiplier = multipliers[unit];
  if (!id || !Number.isInteger(value) || value < 0 || multiplier === undefined) {
    throw new Error("Invalid automation timing.");
  }

  const absoluteMinutes = value * multiplier;
  if (!Number.isSafeInteger(absoluteMinutes)) {
    throw new Error("Invalid automation timing.");
  }

  let offsetMinutes = absoluteMinutes;
  if (triggerKind === "before_appointment") offsetMinutes = -absoluteMinutes;
  if (triggerKind === "appointment_created") offsetMinutes = 0;

  await tiaRequest(`/automations/rules/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ offset_minutes: offsetMinutes }),
  });
  revalidatePath("/automations");
}

export async function resumeWhatsappConnection(formData: FormData) {
  const id = String(formData.get("connection_id") || "").trim();
  if (!id) return;
  await tiaRequest(`/channels/connections/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ status: "active" }),
  });
  revalidatePath("/automations");
}

export async function retryAutomationJob(formData: FormData) {
  const id = String(formData.get("job_id") || "").trim();
  await tiaRequest(`/automations/jobs/${id}/retry`, { method: "POST" });
  revalidatePath("/automations");
}

export async function cancelAutomationJob(formData: FormData) {
  const id = String(formData.get("job_id") || "").trim();
  await tiaRequest(`/automations/jobs/${id}/cancel`, { method: "POST" });
  revalidatePath("/automations");
}