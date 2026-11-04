"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

import { actionFeedbackFailure, type ActionFeedbackState } from "@/lib/action-feedback";
import { tiaRequest } from "@/lib/tia/api";

function numberValue(value: FormDataEntryValue | null) {
  const result = Number(String(value || "0"));
  if (!Number.isFinite(result) || result < 0) throw new Error("اكتب كمية صحيحة.");
  return result;
}

function integerValue(value: FormDataEntryValue | null) {
  const result = numberValue(value);
  if (!Number.isInteger(result)) throw new Error("اكتب عددًا صحيحًا.");
  return result;
}

function positiveNumberValue(value: FormDataEntryValue | null) {
  const result = numberValue(value);
  if (result <= 0) throw new Error("اكتب كمية أكبر من صفر.");
  return result;
}

function positiveIntegerValue(value: FormDataEntryValue | null) {
  const result = integerValue(value);
  if (result <= 0) throw new Error("اكتب عددًا أكبر من صفر.");
  return result;
}

function success(notice: string): ActionFeedbackState {
  return { notice, error: null };
}

export async function createClinicProduct(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const name = String(formData.get("name") || "").trim();
    if (!name) throw new Error("اكتب اسم المنتج.");
    await tiaRequest("/inventory/products", {
      method: "POST",
      body: JSON.stringify({
        name,
        description: String(formData.get("description") || "").trim() || null,
        quantity_on_hand: integerValue(formData.get("quantity_on_hand")),
      }),
    });
    revalidatePath("/inventory");
    revalidatePath("/appointments");
    return success("تمت إضافة المنتج للمخزن بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر إضافة المنتج. حاول مرة أخرى.");
  }
}

export async function addClinicProductStock(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const id = String(formData.get("product_id") || "");
    if (!id) throw new Error("تعذر تحديد المنتج.");
    const quantityAdded = positiveIntegerValue(formData.get("quantity_to_add"));
    await tiaRequest("/inventory/products/" + id + "/stock", {
      method: "POST",
      body: JSON.stringify({ quantity_added: quantityAdded }),
    });
    revalidatePath("/inventory");
    revalidatePath("/appointments");
    return success("تمت إضافة الكمية الجديدة إلى رصيد المنتج.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذرت إضافة الكمية للمخزون. حاول مرة أخرى.");
  }
}

export async function createInventoryItem(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const name = String(formData.get("name") || "").trim();
    if (!name) throw new Error("اكتب اسم الحقنة.");
    await tiaRequest("/inventory/items", {
      method: "POST",
      body: JSON.stringify({
        name,
        quantity_ml: numberValue(formData.get("quantity_ml")),
        low_stock_threshold_ml: formData.get("low_stock_threshold_ml")
          ? numberValue(formData.get("low_stock_threshold_ml"))
          : null,
        notes: String(formData.get("notes") || "").trim() || null,
      }),
    });
    revalidatePath("/inventory");
    return success("تمت إضافة الحقنة للمخزن بنجاح.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذرت إضافة الحقنة. حاول مرة أخرى.");
  }
}

export async function addInventoryStock(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const id = String(formData.get("item_id") || "");
    if (!id) throw new Error("تعذر تحديد الحقنة.");
    await tiaRequest("/inventory/items/" + id + "/stock", {
      method: "POST",
      body: JSON.stringify({ quantity_ml: positiveNumberValue(formData.get("quantity_ml")) }),
    });
    revalidatePath("/inventory");
    return success("تمت إضافة الكمية الجديدة إلى مخزون الحقنة.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذرت إضافة الكمية للمخزون. حاول مرة أخرى.");
  }
}

export async function recordInventoryUsage(
  _previous: ActionFeedbackState,
  formData: FormData,
): Promise<ActionFeedbackState> {
  try {
    const id = String(formData.get("item_id") || "");
    if (!id) throw new Error("تعذر تحديد الحقنة.");
    await tiaRequest("/inventory/items/" + id + "/usage", {
      method: "POST",
      body: JSON.stringify({
        used_ml: positiveNumberValue(formData.get("used_ml")),
        note: String(formData.get("note") || "").trim() || null,
      }),
    });
    revalidatePath("/inventory");
    return success("تم تسجيل الاستخدام وخصم الكمية من المخزون.");
  } catch (error) {
    return actionFeedbackFailure(error, "تعذر تسجيل الاستخدام. راجع الكمية وحاول مرة أخرى.");
  }
}

export async function deleteClinicProduct(formData: FormData): Promise<void> {
  const id = String(formData.get("product_id") || "");
  if (!id) redirect("/inventory?notice=delete_failed");
  try {
    await tiaRequest("/inventory/products/" + id, { method: "DELETE" });
  } catch {
    redirect("/inventory?notice=delete_failed");
  }
  revalidatePath("/inventory");
  revalidatePath("/appointments");
  redirect("/inventory?notice=product_deleted");
}

export async function deleteInventoryItem(formData: FormData): Promise<void> {
  const id = String(formData.get("item_id") || "");
  if (!id) redirect("/inventory?notice=delete_failed");
  try {
    await tiaRequest("/inventory/items/" + id, { method: "DELETE" });
  } catch {
    redirect("/inventory?notice=delete_failed");
  }
  revalidatePath("/inventory");
  redirect("/inventory?notice=item_deleted");
}