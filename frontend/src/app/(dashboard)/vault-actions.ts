"use server";

import { revalidatePath } from "next/cache";
import { actionFeedbackFailure, type ActionFeedbackState } from "@/lib/action-feedback";
import { tiaRequest } from "@/lib/tia/api";

const vaultKinds = new Set(["reception", "main_cash", "main_bank"]);
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function moneyMinor(value: FormDataEntryValue | null): number {
  const raw = String(value ?? "").trim();
  if (!/^\d+(?:\.\d{1,2})?$/.test(raw)) throw new Error("راجع قيمة المبلغ المدخلة.");
  const [whole, fraction = ""] = raw.split(".");
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    throw new Error("المبلغ غير صالح.");
  }
  return amount;
}

function branchId(formData: FormData) {
  const value = String(formData.get("branch_id") || "");
  if (!uuidPattern.test(value)) throw new Error("يجب اختيار فرع صحيح.");
  return value;
}

function idempotencyKey(formData: FormData) {
  const value = String(formData.get("idempotency_key") || "");
  if (!uuidPattern.test(value)) throw new Error("تعذر تحديد العملية. حدّث الصفحة وحاول مرة أخرى.");
  return value;
}

function refreshFinanceAndReception() {
  revalidatePath("/dashboard");
  revalidatePath("/finance");
}

export async function transferReceptionBalance(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const branch = branchId(formData);
    await tiaRequest("/vault/reception/" + branch + "/transfer", {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey(formData) }),
    });
    refreshFinanceAndReception();
    return { notice: "تم توريد العهدة إلى الخزنة الرئيسية بنجاح.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تأكيد نتيجة التوريد. حدّث الرصيد قبل إعادة المحاولة.");
  }
}

export async function recordReceptionExpense(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const branch = branchId(formData);
    const description = String(formData.get("description") || "").trim();
    if (!description || description.length > 200) throw new Error("الوصف مطلوب ويجب ألا يتجاوز 200 حرف.");
    const amount_minor = moneyMinor(formData.get("amount"));
    await tiaRequest("/vault/reception/" + branch + "/expenses", {
      method: "POST",
      body: JSON.stringify({
        amount_minor, description, idempotency_key: idempotencyKey(formData),
      }),
    });
    refreshFinanceAndReception();
    return { notice: "تم تسجيل المصروف وخصم المبلغ من خزنة الريسبشن.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تأكيد نتيجة المصروف. حدّث الرصيد قبل إعادة المحاولة.");
  }
}

export async function recordVaultFunding(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const kind = String(formData.get("kind") || "");
    if (!vaultKinds.has(kind)) throw new Error("نوع الخزنة غير صالح.");
    const rawBranch = String(formData.get("branch_id") || "");
    if (kind !== "reception" && rawBranch) throw new Error("الخزنة الرئيسية لا تتبع فرعًا.");
    const path = kind === "reception"
      ? "/vault/reception/" + branchId(formData) + "/funding"
      : "/vault/treasury/" + kind + "/funding";
    const amount_minor = moneyMinor(formData.get("amount"));
    const note = String(formData.get("note") || "").trim();
    if (amount_minor > 1_000_000_000_000 || note.length > 500) throw new Error("راجع المبلغ والملاحظات.");
    await tiaRequest(path, {
      method: "POST",
      body: JSON.stringify({ amount_minor, note: note || null, idempotency_key: idempotencyKey(formData) }),
    });
    refreshFinanceAndReception();
    return { notice: "تمت إضافة الأموال إلى الخزنة دون تسجيل إيراد.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تأكيد إضافة الأموال. حدّث الرصيد قبل إعادة المحاولة.");
  }
}

export async function recordMainBankDeposit(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    await tiaRequest("/vault/treasury/bank-deposits", {
      method: "POST",
      body: JSON.stringify({
        amount_minor: moneyMinor(formData.get("amount")),
        idempotency_key: idempotencyKey(formData),
      }),
    });
    refreshFinanceAndReception();
    return { notice: "تم توريد الكاش إلى البنك.", error: null };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تأكيد التوريد إلى البنك. حدّث الرصيد قبل إعادة المحاولة.");
  }
}

export async function resolvePendingVaultPayment(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const pending_id = String(formData.get("pending_id") || "");
    if (!uuidPattern.test(pending_id)) throw new Error("الحركة المالية المطلوبة غير صالحة.");
    const branch_id = branchId(formData);
    const explanation = String(formData.get("explanation") || "").trim();
    if (explanation.length < 8 || explanation.length > 500) {
      throw new Error("اكتب سبب المراجعة والمستند الذي يثبت ملكية الفرع (8–500 حرف).");
    }
    await tiaRequest("/vault/pending-payments/" + pending_id + "/resolve", {
      method: "POST",
      body: JSON.stringify({ branch_id, explanation }),
    });
    refreshFinanceAndReception();
    return {
      notice: "تمت مراجعة الدفعة وترحيلها إلى الفرع الذي تثبته سجلات التخصيص.",
      error: null,
    };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تأكيد التسوية. حدّث الصفحة قبل إعادة المحاولة.");
  }
}


export async function reviewElectronicOpeningReceipt(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const pending_id = String(formData.get("pending_id") || "");
    if (!uuidPattern.test(pending_id)) throw new Error("مرجع الدفعة غير صالح.");
    const outcome = String(formData.get("outcome") || "");
    if (outcome !== "included_in_opening" && outcome !== "requires_accounting_decision") {
      throw new Error("اختر نتيجة المراجعة.");
    }
    const explanation = String(formData.get("explanation") || "").trim();
    if (explanation.length < 8 || explanation.length > 500) {
      throw new Error("اكتب دليل التسوية (8–500 حرف).");
    }
    await tiaRequest("/vault/pending-payments/" + pending_id + "/electronic-opening-review", {
      method: "POST",
      body: JSON.stringify({ outcome, explanation }),
    });
    refreshFinanceAndReception();
    return {
      notice: outcome === "included_in_opening"
        ? "تم توثيق أن المبلغ مشمول في رصيد افتتاحي تاريخي مسجل، دون إضافة تحصيل جديد."
        : "تم توثيق الحالة كمعلقة لقرار محاسبي، دون ترحيل أي مبلغ.",
      error: null,
    };
  } catch (error) {
    return actionFeedbackFailure(error, "لم تُؤكَّد المراجعة؛ حدّث البيانات قبل المحاولة.");
  }
}


export async function retryEligibleElectronicReceipt(
  _previous: ActionFeedbackState, formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const pending_id = String(formData.get("pending_id") || "");
    if (!uuidPattern.test(pending_id)) throw new Error("مرجع الدفعة غير صالح.");
    const explanation = String(formData.get("explanation") || "").trim();
    if (explanation.length < 8 || explanation.length > 500) {
      throw new Error("اكتب مبرر إعادة الترحيل (8–500 حرف).");
    }
    await tiaRequest("/vault/pending-payments/" + pending_id + "/electronic-retry", {
      method: "POST", body: JSON.stringify({ explanation }),
    });
    refreshFinanceAndReception();
    return {
      notice: "تم ترحيل الدفعة الإلكترونية المؤهلة مرة واحدة بعد مراجعة تاريخ التفعيل.",
      error: null,
    };
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تأكيد الترحيل؛ حدّث الرصيد قبل إعادة المحاولة.");
  }
}
