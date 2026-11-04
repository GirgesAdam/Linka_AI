"use server";

import { revalidatePath } from "next/cache";

import { actionFeedbackFailure, type ActionFeedbackState } from "@/lib/action-feedback";
import { tiaRequest } from "@/lib/tia/api";

function refreshTaskViews(patientId?: string) {
  revalidatePath("/dashboard");
  revalidatePath("/tasks");
  if (patientId) revalidatePath("/patients/" + patientId);
}

export async function claimTask(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const taskId = String(formData.get("task_id") || "");
    const patientId = String(formData.get("patient_id") || "");
    if (!taskId) throw new Error("تعذر تحديد المتابعة.");
    await tiaRequest("/crm/tasks/" + taskId + "/claim", { method: "POST" });
    refreshTaskViews(patientId || undefined);
    return { notice: "تم استلام المتابعة بنجاح.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر استلام المتابعة. حاول مرة أخرى.");
  }
}

export async function setTaskStatus(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const taskId = String(formData.get("task_id") || "");
    const patientId = String(formData.get("patient_id") || "");
    const status = String(formData.get("status") || "");
    if (!taskId || !["completed", "cancelled", "pending", "in_progress"].includes(status)) {
      throw new Error("بيانات تحديث المتابعة غير صحيحة.");
    }
    await tiaRequest("/crm/tasks/" + taskId, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    refreshTaskViews(patientId || undefined);
    return {
      notice: status === "completed" ? "تم تسجيل المتابعة كمكتملة." : status === "cancelled" ? "تم إلغاء المتابعة." : "تم تحديث حالة المتابعة.",
      error: null,
    };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تحديث حالة المتابعة. حاول مرة أخرى.");
  }
}

export async function assignTask(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const taskId = String(formData.get("task_id") || "");
    const patientId = String(formData.get("patient_id") || "");
    const executor = String(formData.get("executor") || "unassigned");
    if (!taskId) throw new Error("تعذر تحديد المتابعة.");

    const body = executor === "tia"
      ? { executor: "tia" }
      : executor.startsWith("staff:") && executor.length > "staff:".length
        ? { executor: "staff", assigned_user_id: executor.slice("staff:".length) }
        : executor === "unassigned"
          ? { executor: "unassigned" }
          : null;
    if (!body) throw new Error("اختار مسؤول متابعة صحيح.");

    await tiaRequest("/crm/tasks/" + taskId + "/executor", {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    refreshTaskViews(patientId || undefined);
    return { notice: "تم تحديث مسؤول المتابعة بنجاح.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تحديث مسؤول المتابعة. حاول مرة أخرى.");
  }
}