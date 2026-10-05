"use server";

import { revalidatePath } from "next/cache";
import { tiaRequest } from "@/lib/tia/api";

function refreshTaskViews(patientId?: string) {
  revalidatePath("/dashboard");
  revalidatePath("/tasks");
  if (patientId) revalidatePath(`/patients/${patientId}`);
}

export async function claimTask(formData: FormData) {
  const taskId = String(formData.get("task_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  if (!taskId) return;
  await tiaRequest(`/crm/tasks/${taskId}/claim`, { method: "POST" });
  refreshTaskViews(patientId || undefined);
}

export async function setTaskStatus(formData: FormData) {
  const taskId = String(formData.get("task_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const status = String(formData.get("status") || "");
  if (!taskId || !status) return;
  await tiaRequest(`/crm/tasks/${taskId}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  refreshTaskViews(patientId || undefined);
}

export async function assignTask(formData: FormData) {
  const taskId = String(formData.get("task_id") || "");
  const patientId = String(formData.get("patient_id") || "");
  const executor = String(formData.get("executor") || "unassigned");
  if (!taskId) return;

  const body = executor === "tia"
    ? { executor: "tia" }
    : executor.startsWith("staff:")
      ? { executor: "staff", assigned_user_id: executor.slice("staff:".length) }
      : { executor: "unassigned" };

  await tiaRequest(`/crm/tasks/${taskId}/executor`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  refreshTaskViews(patientId || undefined);
}
