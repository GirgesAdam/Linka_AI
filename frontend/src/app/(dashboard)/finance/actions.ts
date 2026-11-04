"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { actionFeedbackFailure, type ActionFeedbackState } from "@/lib/action-feedback";
import { tiaRequest } from "@/lib/tia/api";

const expenseCategories = new Set([
  "rent",
  "payroll",
  "supplies",
  "marketing",
  "utilities",
  "maintenance",
  "software",
  "taxes",
  "other",
]);
const expenseTypes = new Set(["fixed", "variable"]);

function parseAmountMinor(value: FormDataEntryValue | null) {
  const raw = String(value || "").trim().replace(",", ".");
  if (!/^\d+(?:\.\d{1,2})?$/.test(raw)) return null;
  const [whole, fraction = ""] = raw.split(".");
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return Number.isSafeInteger(amount) && amount > 0 ? amount : null;
}

function expensePayload(formData: FormData) {
  const title = String(formData.get("title") || "").trim();
  const category = String(formData.get("category") || "other");
  const expenseType = String(formData.get("expense_type") || "variable");
  const amountMinor = parseAmountMinor(formData.get("amount"));
  const incurredOn = String(formData.get("incurred_on") || "").trim();
  const note = String(formData.get("note") || "").trim();

  if (
    !title ||
    !expenseCategories.has(category) ||
    !expenseTypes.has(expenseType) ||
    !amountMinor ||
    !/^\d{4}-\d{2}-\d{2}$/.test(incurredOn)
  ) {
    throw new Error("راجع اسم المصروف والتصنيف والمبلغ والتاريخ قبل الحفظ.");
  }

  return {
    title,
    category,
    expense_type: expenseType,
    amount_minor: amountMinor,
    currency: "EGP",
    incurred_on: incurredOn,
    note: note || null,
  };
}

export async function createExpense(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const payload = expensePayload(formData);
    await tiaRequest("/finance/expenses", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    revalidatePath("/finance");
    return { notice: "تم تسجيل المصروف بنجاح.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذرت إضافة المصروف. حاول مرة أخرى.");
  }
}

export async function updateExpense(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const expenseId = String(formData.get("expense_id") || "").trim();
    if (!expenseId) throw new Error("تعذر تحديد المصروف المطلوب تعديله.");
    const payload = expensePayload(formData);
    await tiaRequest("/finance/expenses/" + expenseId, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    revalidatePath("/finance");
    return { notice: "تم حفظ تعديل المصروف بنجاح.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر حفظ تعديل المصروف. حاول مرة أخرى.");
  }
}

export async function deleteExpense(formData: FormData): Promise<void> {
  const expenseId = String(formData.get("expense_id") || "").trim();
  const startDate = String(formData.get("return_start_date") || "").trim();
  const endDate = String(formData.get("return_end_date") || "").trim();
  const query = new URLSearchParams();
  if (startDate) query.set("start_date", startDate);
  if (endDate) query.set("end_date", endDate);

  if (!expenseId) {
    query.set("notice", "delete_failed");
    redirect("/finance?" + query.toString());
  }
  try {
    await tiaRequest("/finance/expenses/" + expenseId, { method: "DELETE" });
  } catch {
    query.set("notice", "delete_failed");
    redirect("/finance?" + query.toString());
  }
  revalidatePath("/finance");
  query.set("notice", "expense_deleted");
  redirect("/finance?" + query.toString());
}