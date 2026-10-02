"use server";

import { revalidatePath } from "next/cache";

import type { ClinicKnowledgeText } from "@/lib/clinic-knowledge-base-types";
import type { ClinicHour, ClinicSetupV2Snapshot } from "@/lib/clinic-setup-v2-types";
import type { ClinicLaserDevice } from "@/lib/types";
import { tiaRequest } from "@/lib/tia/api";

function refresh() {
  revalidatePath("/setup");
  revalidatePath("/dashboard");
}

function text(formData: FormData, key: string) {
  const value = String(formData.get(key) ?? "").trim();
  return value || null;
}

export async function saveClinicProfileFormAction(formData: FormData) {
  const name = text(formData, "name");
  if (!name) throw new Error("Ø§Ø³Ù… Ø§Ù„Ø¹ÙŠØ§Ø¯Ø© Ù…Ø·Ù„ÙˆØ¨.");
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
}

export async function saveClinicHoursFormAction(formData: FormData) {
  const intervals: ClinicHour[] = [];
  for (let weekday = 0; weekday < 7; weekday += 1) {
    if (formData.get(`enabled_${weekday}`) !== "on") continue;
    const start = text(formData, `start_${weekday}`);
    const end = text(formData, `end_${weekday}`);
    if (!start || !end) throw new Error("Ø­Ø¯Ø¯ Ø¨Ø¯Ø§ÙŠØ© ÙˆÙ†Ù‡Ø§ÙŠØ© Ù…ÙˆØ§Ø¹ÙŠØ¯ ÙƒÙ„ ÙŠÙˆÙ… Ù…ÙØ¹Ù‘Ù„.");
    intervals.push({ weekday, start_time: start, end_time: end });
  }
  await tiaRequest<ClinicSetupV2Snapshot>("/clinic/setup-v2/hours", {
    method: "PUT",
    body: JSON.stringify({ intervals }),
  });
  refresh();
}

export async function addLaserDeviceFormAction(formData: FormData) {
  const name = text(formData, "name");
  if (!name) throw new Error("اكتب اسم الجهاز.");
  await tiaRequest<ClinicLaserDevice>("/inventory/laser-devices", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  revalidatePath("/setup");
  revalidatePath("/services");
  revalidatePath("/appointments");
}

export async function updateLaserDeviceFormAction(formData: FormData) {
  const deviceId = String(formData.get("device_id") || "").trim();
  if (!deviceId) return;
  const name = text(formData, "name");
  await tiaRequest<ClinicLaserDevice>(`/inventory/laser-devices/${deviceId}`, {
    method: "PATCH",
    body: JSON.stringify({
      name,
      is_active: formData.get("is_active") === "on",
    }),
  });
  revalidatePath("/setup");
  revalidatePath("/services");
  revalidatePath("/appointments");
}

export async function saveKnowledgeTextFormAction(formData: FormData) {
  const content = String(formData.get("content") ?? "").trim();
  await tiaRequest<ClinicKnowledgeText>("/clinic/knowledge-text", {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
  refresh();
}
